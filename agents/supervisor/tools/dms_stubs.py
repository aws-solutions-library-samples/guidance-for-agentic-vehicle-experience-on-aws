"""DMS stub tools for v1.1-deferred personas: ``parts_intel`` and
``compliance_agent``.

These stubs exist so supervisor dispatch cannot raise a KeyError when a
``persona_id`` the UI can produce maps to one of these slugs.  A stub that
returns an explicit deferral shape is honest; a missing key raises inside a
conversational turn.

Contract (spec § Personas):

    {"available": False,
     "reason": "v1.1 deferred — see spec 2026-08-26-cvx-dms-personas-extension § Personas",
     "persona": "parts_intel" | "compliance_agent"}

The ``persona`` key names which stub replied — a supervisor whose only signal
is a stub payload can distinguish between them without inspecting the tool
metadata.  A truncated reason ("v1.1 deferred") satisfies a substring grep
but drops the spec pointer, which is the only thing that tells a reader
*where* to find the deferral rationale.

NO LLM call inside any tool.  NO DDB read.  NO network I/O.  A partial
implementation would be indistinguishable from an active tool at inspection
time — the whole point of the stub is inertness.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

# Full deferral message.  A substring grep for "v1.1 deferred" still matches
# — but a reader following the reason string reaches the spec that owns the
# deferral decision.  Keep the citation intact.
_DEFERRED_MSG = (
    "v1.1 deferred — see spec 2026-08-26-cvx-dms-personas-extension § Personas"
)


def make_parts_intel_stub_tool(ctx: SessionContext, config: Config):
    """Build the ``parts_intel`` stub tool.

    Returns the explicit v1.1 deferral shape so the supervisor can narrate
    "parts intelligence is not yet available" rather than raising an
    exception on dispatch.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that returns the deferral payload.
    """

    @tool
    def parts_intel_lookup(query: str = "") -> dict[str, Any]:
        """Look up parts intelligence data for a dealer — cross-dealer parts
        availability, slow-moving inventory flags, and reorder
        recommendations.

        NOTE: This capability is deferred to v1.1.  The tool is present so
        the system does not error on a ``parts_intel`` persona request.  A
        v1.1 implementation should replace both this function and the
        factory return; it MUST NOT augment the stub with partial reads.

        Args:
            query: Optional parts query string (reserved for v1.1).

        Returns:
            ``{"available": False, "reason": <deferred>, "persona": "parts_intel"}``.
        """
        return {
            "available": False,
            "reason": _DEFERRED_MSG,
            "persona": "parts_intel",
        }

    return parts_intel_lookup


def make_compliance_agent_stub_tool(ctx: SessionContext, config: Config):
    """Build the ``compliance_agent`` stub tool.

    Returns the explicit v1.1 deferral shape so the supervisor can narrate
    "compliance checking is not yet available" rather than raising an
    exception on dispatch.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that returns the deferral payload.
    """

    @tool
    def compliance_check(dealer_id: str = "") -> dict[str, Any]:
        """Check compliance status for a dealer — regulatory filings,
        franchise agreement adherence, and certification-score gate status.

        NOTE: This capability is deferred to v1.1.

        Args:
            dealer_id: Optional dealer identifier (reserved for v1.1).

        Returns:
            ``{"available": False, "reason": <deferred>, "persona":
              "compliance_agent"}``.
        """
        return {
            "available": False,
            "reason": _DEFERRED_MSG,
            "persona": "compliance_agent",
        }

    return compliance_check
