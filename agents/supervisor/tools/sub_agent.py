"""Sub-agent tool factory — retrieval-only variant.

A "sub-agent" in this codebase is a scoped retrieval tool: Nova
delegates a domain-specific question to it (e.g. DIY repair, recall
lookup), the tool returns the relevant corpus content, and Nova
synthesizes the spoken response herself.

We deliberately do NOT run a downstream LLM (Claude / Sonnet) inside
the tool, because Nova Sonic v2's bidi loop assumes tool calls return
within ~1s. Anything slower (Sonnet ≈3s, Haiku ≈2s) makes Nova lose
its post-tool TTS — empirically proven 2026-05-22 with both Sonnet
4.5 and Haiku 4.5. Returning bare KB content keeps the tool fast
(<10ms) and preserves Nova's TTS reliability; in exchange Nova does
the synthesis with her own model. For the (i) button text path this
already works fine, so we trust it for sub-agent-style routing too.

Each sub-agent has:

  - A scoped tool name and description (drives Nova's routing)
  - A specific corpus subdirectory under `corpora/` it consults
  - The supervisor system prompt should instruct Nova how to
    consume the result (see `bidi_app_canonical.py`'s SYSTEM_PROMPT)

Corpus loading is intentionally simple: load all .md files from the
configured subdirectory into one big block per call, cached at module
import. Corpora are small (single-digit KB) so token cost is trivial.
Switch to a real KB (Bedrock Knowledge Base, OpenSearch, etc.) once
the corpus exceeds ~50 KB.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from strands import tool

from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPORA_ROOT = REPO_ROOT / "corpora"

# Cap on bytes returned to Nova. Big tool results bloat the model
# context and slow Nova's response. Owner-manuals corpus is ~5KB
# total so this rarely truncates, but the safety net matters when
# we add more sub-agents pointed at larger corpora.
MAX_REFERENCE_BYTES = 8000

_CORPUS_CACHE: dict[str, str] = {}


def _load_corpus(subdir: str) -> str:
    if subdir in _CORPUS_CACHE:
        return _CORPUS_CACHE[subdir]
    dir_path = CORPORA_ROOT / subdir
    if not dir_path.is_dir():
        log.warning("sub_agent: corpus subdir %s not found", dir_path)
        _CORPUS_CACHE[subdir] = ""
        return ""
    parts: list[str] = []
    for md in sorted(dir_path.glob("*.md")):
        try:
            parts.append(f"=== {md.name} ===\n{md.read_text()}\n")
        except OSError as e:
            log.warning("sub_agent: failed to read %s: %s", md, e)
    text = "\n".join(parts)
    _CORPUS_CACHE[subdir] = text
    log.info(
        "sub_agent: loaded corpus subdir=%s files=%d size=%d",
        subdir, len(parts), len(text),
    )
    return text


@dataclass
class SubAgentConfig:
    """Configuration for one sub-agent retrieval tool."""

    name: str  # tool name Nova calls (e.g. "diy_repair_advisor")
    description: str  # tool description shown to Nova for routing
    corpus_subdir: str  # directory under corpora/ to load
    # Optional: a function that, given the query/dtc/vehicle context,
    # returns a markdown-formatted chat message to render on screen
    # alongside Nova's voice response. Used to push detailed reference
    # data (e.g. tire pressure tables) to the screen so Nova can stay
    # brief in voice. Return None if no chat message is appropriate.
    chat_message_fn: Any = None


def make_sub_agent_tool(
    config: SubAgentConfig,
    ctx: SessionContext | None = None,
) -> Callable:
    """Build a Strands @tool that retrieves corpus content for Nova.

    Returns a `@tool`-decorated function. The tool returns a dict with:
      - reference_material: corpus content scoped to this sub-agent's
        domain (Nova should ground its response in this)
      - source: which corpus subdir the content came from
      - vehicle, driver, dtc_code: passthrough context Nova may use
        in its synthesized response

    Nova synthesizes the spoken reply herself using its bidi inference.
    The supervisor system prompt is responsible for telling Nova how
    to consume the reference_material (e.g. "summarize in 2 sentences
    for voice").
    """
    name = config.name
    description = config.description
    corpus_subdir = config.corpus_subdir

    @tool(name=name, description=description)
    def sub_agent_tool(query: str, dtc_code: str = "") -> dict[str, Any]:
        """Retrieve scoped reference material for a domain question."""
        query = (query or "").strip()
        dtc_code = (dtc_code or "").strip()

        corpus = _load_corpus(corpus_subdir)
        if len(corpus) > MAX_REFERENCE_BYTES:
            corpus = corpus[:MAX_REFERENCE_BYTES] + "\n\n[truncated]"

        vehicle_phrase = ""
        driver_first = ""
        if ctx is not None:
            try:
                from agents.supervisor.bidi_app import (
                    _fetch_vehicle_context, _fetch_driver_context,
                )
                if ctx.vehicle_id:
                    vc = _fetch_vehicle_context(ctx.vehicle_id) or {}
                    vehicle_phrase = " ".join(
                        str(p) for p in (vc.get("year"), vc.get("make"), vc.get("model"))
                        if p
                    )
                if ctx.driver_id:
                    dc = _fetch_driver_context(ctx.driver_id) or {}
                    driver_first = dc.get("firstName") or ""
            except Exception as e:  # noqa: BLE001
                log.warning("sub_agent %s: context fetch failed: %s", name, e)

        log.info(
            "sub_agent %s: returning reference_material chars=%d query=%r dtc=%r",
            name, len(corpus), query[:80], dtc_code,
        )
        result: dict[str, Any] = {
            "reference_material": corpus,
            "source": corpus_subdir,
            "vehicle": vehicle_phrase or None,
            "driver": driver_first or None,
            "dtc_code": dtc_code or None,
            "query": query,
        }
        # Optional: render a screen-side chat message with detailed
        # reference data so Nova can stay brief in voice.
        if config.chat_message_fn is not None:
            try:
                cm = config.chat_message_fn(
                    query=query,
                    dtc_code=dtc_code,
                    vehicle_phrase=vehicle_phrase,
                    driver_first=driver_first,
                )
                if cm:
                    result["chat_message"] = cm
                    log.info(
                        "sub_agent %s: chat_message attached chars=%d",
                        name, len(cm),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "sub_agent %s: chat_message_fn raised: %s — skipping",
                    name, e,
                )
        return result

    return sub_agent_tool
