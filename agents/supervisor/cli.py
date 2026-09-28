"""CLI for the VSA supervisor.

Two modes:

  python -m agents.supervisor.cli
      Interactive REPL. Type messages, see responses + tool-call traces.

  python -m agents.supervisor.cli --scenario=p1_coolant_overtemp
      Run a scripted scenario from tests/scenarios/<name>.json.
      Prints the tool-call sequence and the final response.

The CLI is a debug tool, not a deployment artifact. The iOS app and
AgentCore will call the supervisor via a different entry point.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from agents.supervisor.config import Config, clear_cached_tokens, get_cached_id_token
from agents.supervisor.session import SessionContext
from agents.supervisor.supervisor import build_supervisor


def main() -> int:
    parser = argparse.ArgumentParser(prog="agents.supervisor.cli")
    parser.add_argument(
        "--scenario",
        help="Name of a JSON scenario file in tests/scenarios/ (without .json)",
    )
    parser.add_argument(
        "--vin",
        help="VIN to seed into the session. Defaults to VSA_DEFAULT_VIN.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = Config.from_env()

    # Dev escape hatch for expired tokens.
    if os.environ.get("VSA_CLEAR_TOKENS"):
        clear_cached_tokens()

    print(f"→ Tenant: {config.tenant_id}")
    print(f"→ API:    {config.api_base}")
    print(f"→ Model:  {config.supervisor_model_id}")

    try:
        jwt = get_cached_id_token(config)
    except Exception as e:  # noqa: BLE001
        print(f"✗ Failed to get Cognito token: {e}", file=sys.stderr)
        return 2

    vin = args.vin or config.default_vin
    ctx = SessionContext.new(tenant_id=config.tenant_id, jwt=jwt, persona_id="fleet_driver", vin=vin)
    print(f"→ Session: {ctx.session_id}, VIN: {ctx.vin}")
    print()

    agent = build_supervisor(ctx, config)

    if args.scenario:
        return _run_scenario(agent, args.scenario)
    return _run_interactive(agent)


def _run_interactive(agent) -> int:
    print("Interactive mode. Ctrl-D or type 'quit' to exit.")
    print()
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit"}:
            return 0
        try:
            result = agent(user_input)
        except Exception as e:  # noqa: BLE001
            print(f"✗ Agent error: {e}")
            continue
        print(f"agent> {_extract_text(result)}")
        print()


def _run_scenario(agent, scenario_name: str) -> int:
    scenarios_dir = Path(__file__).parent / "tests" / "scenarios"
    path = scenarios_dir / f"{scenario_name}.json"
    if not path.exists():
        print(f"✗ Scenario not found: {path}", file=sys.stderr)
        available = sorted(p.stem for p in scenarios_dir.glob("*.json"))
        print(f"  Available: {available}", file=sys.stderr)
        return 2

    scenario = json.loads(path.read_text())
    print(f"→ Scenario: {scenario['name']}")
    print()

    for turn_idx, user_turn in enumerate(scenario["user_turns"], 1):
        print(f"[turn {turn_idx}] you> {user_turn}")
        try:
            result = agent(user_turn)
        except Exception as e:  # noqa: BLE001
            print(f"✗ Agent error: {e}")
            return 1
        print(f"[turn {turn_idx}] agent> {_extract_text(result)}")
        print()

    return 0


def _extract_text(result) -> str:
    """Pull the final text response out of an AgentResult."""
    msg = getattr(result, "message", None)
    if msg is None:
        return str(result)
    # Strands AgentResult.message is a Message with content blocks.
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not content:
        return str(msg)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    return "\n".join(parts) if parts else str(msg)


if __name__ == "__main__":
    sys.exit(main())
