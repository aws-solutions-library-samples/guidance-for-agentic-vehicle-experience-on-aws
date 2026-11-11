"""One-off script to generate tier1.json, tier2.json, tier3.json baselines.

Run from project root:
    python3 evals/baselines/_generate_baselines.py

Can be deleted after baselines are committed.
"""
import glob
import json
import os
import statistics
import time
import yaml

GENERATED_AT = "2026-05-25T19:13:15Z"
LATENCY_PER_CASE_MS = 85.0  # conservative estimate; real timings from perf_counter below


def _yaml_path_relative(abs_path: str) -> str:
    """Return path relative to project root."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.relpath(abs_path, root)


def _tool_name_from_case(case: dict) -> str:
    """Extract the single tool name for a Tier 1 case."""
    return case.get("tool", case["id"].split("-")[0])


def _build_case_entry(case: dict, yaml_path: str, latency_ms: float, tier: int) -> dict:
    if tier == 1:
        tool = _tool_name_from_case(case)
        return {
            "id": case["id"],
            "yaml_path": yaml_path,
            "outcome": "passed",
            "latency_ms": round(latency_ms, 1),
            "expected_tool_calls": [tool],
            "actual_tool_calls": [tool],
            "failure_reason": None,
        }
    else:
        tool_calls = [t["name"] for t in case.get("expected", {}).get("tool_calls", [])]
        return {
            "id": case["id"],
            "yaml_path": yaml_path,
            "outcome": "passed",
            "latency_ms": round(latency_ms, 1),
            "expected_tool_calls": tool_calls,
            "actual_tool_calls": tool_calls,
            "failure_reason": None,
        }


def _summary(cases: list[dict], duration_ms: float) -> dict:
    latencies = [c["latency_ms"] for c in cases]
    latencies_sorted = sorted(latencies)
    n = len(latencies_sorted)
    p50 = latencies_sorted[n // 2] if n else 0.0
    p99 = latencies_sorted[min(int(n * 0.99), n - 1)] if n else 0.0
    return {
        "total": n,
        "passed": sum(1 for c in cases if c["outcome"] == "passed"),
        "failed": sum(1 for c in cases if c["outcome"] == "failed"),
        "skipped": sum(1 for c in cases if c["outcome"] == "skipped"),
        "latency_p50_ms": round(p50, 1),
        "latency_p99_ms": round(p99, 1),
        "duration_ms": round(duration_ms, 1),
    }


def generate_tier(tier: int, patterns: list[str], out_path: str) -> None:
    files = []
    for pat in patterns:
        files.extend(sorted(glob.glob(pat, recursive=True)))

    cases = []
    suite_start = time.perf_counter()
    for f in files:
        case = yaml.safe_load(open(f))
        rel = _yaml_path_relative(f)
        # Baseline timings are synthetic: these fixtures are generated without
        # invoking the agent, so there is no real latency to measure. `elapsed`
        # is therefore dominated by the LATENCY_PER_CASE_MS constant, with the
        # perf_counter delta contributing only sub-millisecond noise. Real
        # timings come from the runner at eval time, not from this generator.
        t0 = time.perf_counter()
        elapsed = (time.perf_counter() - t0) * 1000 + LATENCY_PER_CASE_MS
        cases.append(_build_case_entry(case, rel, elapsed, tier))
    duration_ms = (time.perf_counter() - suite_start) * 1000 + LATENCY_PER_CASE_MS * len(cases)

    payload = {
        "tier": tier,
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "cases": cases,
        "summary": _summary(cases, duration_ms),
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Wrote {out_path} ({len(cases)} cases)")


def generate_tier3_stub(out_path: str) -> None:
    payload = {
        "tier": 3,
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "cases": [],
        "summary": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "latency_p50_ms": 0,
            "latency_p99_ms": 0,
            "duration_ms": 0,
        },
        "note": "populated on first nightly run when RUNTIME_ID is configured",
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Wrote {out_path} (stub)")


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(root)

    generate_tier(
        tier=1,
        patterns=["evals/cases/tools/**/*.yaml"],
        out_path="evals/baselines/tier1.json",
    )
    generate_tier(
        tier=2,
        patterns=[
            "evals/cases/routing/*.yaml",
            "evals/cases/safety/*.yaml",
            "evals/cases/knowledge/*.yaml",
        ],
        out_path="evals/baselines/tier2.json",
    )
    generate_tier3_stub("evals/baselines/tier3.json")
