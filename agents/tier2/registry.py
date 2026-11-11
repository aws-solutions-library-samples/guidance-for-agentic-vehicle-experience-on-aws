"""Pluggable agent-module registry for the Tier 2 platform.

Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md § Group 2,
      task "Generalise runner.py to a pluggable registered-modules pattern".

Each entry in ``AGENT_MODULES`` bundles everything the runner needs to
dispatch to one Tier 2 agent:

``run_agent``
    The deterministic fallback callable — ``(vin, rows) -> dict``.
    Must be importable without Strands / Bedrock.  Used by the runner
    when ``agent_fn`` is explicitly injected (tests) or when
    ``make_strands_agent_fn`` raises ``ImportError``.

``SYSTEM_PROMPT``
    The system prompt string the Strands agent is initialised with.
    Referenced here so the registry is self-contained; the canonical
    definition lives in the agent's own prompt module.

``valid_components``
    ``frozenset[str]`` of the component names this agent recognises.
    The runner validates ``event["component"]`` against this set.

``make_strands_agent_fn``
    Zero-or-one-argument factory that returns a ``(vin, rows) -> dict``
    callable backed by a live Strands / Bedrock model.  Raises
    ``ImportError`` when strands is not installed; the runner catches
    that and falls back to ``run_agent``.

``artifact_table_env_var``
    Name of the environment variable that holds the DynamoDB table name
    for this agent's output artifacts.  The runner uses this to wire the
    production ``ArtifactWriter`` without hard-coding a table name.

Shape (``AgentModule``)
-----------------------
``AgentModule`` is a plain ``TypedDict`` so callers can annotate without
importing a heavyweight class hierarchy.  Spec 1b adds further entries to
``AGENT_MODULES`` without touching this file.

Zero behavioural change
-----------------------
Agent 2's ``run_agent`` / ``SYSTEM_PROMPT`` / ``valid_components`` /
``make_strands_agent_fn`` / ``artifact_table_env_var`` are forwarded from
the existing ``agents.health`` namespace — no logic is duplicated here.
The only change visible to ``runner.py`` is that ``_default_agent_fn``
now routes through this registry instead of calling ``run_health_agent``
directly.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict


# ---------------------------------------------------------------------------
# AgentModule shape
# ---------------------------------------------------------------------------

class AgentModule:  # noqa: D101 — public API, documented in module docstring
    """Descriptor for one registered Tier 2 agent module.

    Instantiated once per entry; all fields are read-only after construction.
    """

    __slots__ = (
        "run_agent",
        "SYSTEM_PROMPT",
        "valid_components",
        "make_strands_agent_fn",
        "artifact_table_env_var",
        "avx_findings_table_env_var",
    )

    def __init__(
        self,
        *,
        run_agent: Callable[[str, list[dict]], dict],
        SYSTEM_PROMPT: str,  # noqa: N803 — intentional UPPER_CASE to match source
        valid_components: frozenset,
        make_strands_agent_fn: Callable[..., Callable[[str, list[dict]], dict]],
        artifact_table_env_var: str,
        avx_findings_table_env_var: str,
    ) -> None:
        self.run_agent = run_agent
        self.SYSTEM_PROMPT = SYSTEM_PROMPT
        self.valid_components = valid_components
        self.make_strands_agent_fn = make_strands_agent_fn
        self.artifact_table_env_var = artifact_table_env_var
        self.avx_findings_table_env_var = avx_findings_table_env_var


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _build_agent_2() -> AgentModule:
    """Construct the Agent 2 (Vehicle Health) module entry.

    Imports are deferred so that the registry can be imported in test
    environments that do not have strands / boto3 on the path.
    """
    from agents.health.agent import run_health_agent, make_strands_agent_fn
    from agents.health.prompt import SYSTEM_PROMPT
    from agents.tier2.contract import VALID_COMPONENTS

    return AgentModule(
        run_agent=run_health_agent,
        SYSTEM_PROMPT=SYSTEM_PROMPT,
        valid_components=VALID_COMPONENTS,
        make_strands_agent_fn=make_strands_agent_fn,
        artifact_table_env_var="HEALTH_FINDINGS_TABLE",
        avx_findings_table_env_var="AVX_FINDINGS_TABLE",
    )


#: Registered Tier 2 agent modules, keyed by stable agent-ID string.
#:
#: Key convention: ``"agent-{N}-{slug}"`` where *N* is the spec-level agent
#: number and *slug* is a short kebab-case descriptor.  Spec 1b appends
#: further entries (e.g. ``"agent-1-lite-energy"``, ``"agent-3-ownership"``).
#:
#: Only Agent 2 is present in Spec 1a.
AGENT_MODULES: dict[str, AgentModule] = {
    "agent-2-vehicle-health": _build_agent_2(),
}
