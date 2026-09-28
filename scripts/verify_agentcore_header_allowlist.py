#!/usr/bin/env python3
"""Fail closed when a custom header the runtime READS is not ALLOWLISTED for delivery.

The gap this closes
-------------------
`.bedrock_agentcore.yaml` carries `request_header_configuration.requestHeaderAllowlist`,
and **AgentCore silently drops any header not on it** — no error, no warning, no log
line. Server-side code reading that header sees `None`, indistinguishable from a client
that never sent it.

That file is **gitignored** (`.gitignore:26`) because it holds per-developer runtime IDs,
so it cannot carry the allowlist as committed state. Every developer and every deploy
path must add new header names to their own copy by hand. Until now the only mitigation
was a paragraph in `docs/tech.md`, and this session has repeatedly demonstrated that a
rule with no executable form is not a control — spec D5 was prose in `config.py` and got
violated four times without anything failing.

This script is the executable form.

What it does
------------
1. **Derives** the set of headers the runtime reads, by AST-scanning the entrypoint for
   module-level constants whose value is a custom-prefixed string or tuple of strings.
   Derived, not hand-listed, so the requirement cannot drift from the code — a
   hand-maintained manifest would be a second thing to forget to update.
2. Reads the local `.bedrock_agentcore.yaml` allowlist for the target runtime.
3. A header group is **covered** when ANY of its accepted casings matches a lowercased
   allowlist entry. That mirrors the server's own tolerance: AgentCore's
   camelCase-to-delivered-key normalization is non-deterministic, so the code accepts
   three spellings and the wire carries one.
4. Classifies every uncovered group against `_KNOWN_GAPS` below:
   * uncovered and **declared** → reported, exit 0 (a deliberate, recorded gap)
   * uncovered and **undeclared** → **FATAL**
   * **covered but declared a gap** → **FATAL**, so the gap list cannot rot into a lie
     once someone fixes one. Same ratchet discipline as
     `test_d5_memory_id_getattr_census.py`.

Usage
-----
    python3 scripts/verify_agentcore_header_allowlist.py                  # staging bidi
    python3 scripts/verify_agentcore_header_allowlist.py --runtime vsa_supervisor_bidi
    python3 scripts/verify_agentcore_header_allowlist.py --json

Exit codes: 0 clean (declared gaps allowed) · 1 violation · 2 cannot check (missing file,
unknown runtime, unparseable entrypoint) — deliberately NOT 0, because "I could not
check" must never read as "it is fine".
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
CONFIG = REPO / ".bedrock_agentcore.yaml"
PREFIX = "x-amzn-bedrock-agentcore-runtime-custom-"

#: Entrypoint per runtime, so the derived set matches what that runtime actually runs.
#: The HTTP/text runtimes are deliberately absent: the
#: `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*` namespace is documented for WebSocket
#: runtimes only, and `vsa_supervisor_text_staging` correspondingly has
#: `request_header_configuration: null`. Asking this question of a text runtime is a
#: category error, and `--runtime` rejects it rather than reporting a false gap.
_ENTRYPOINTS: dict[str, str] = {
    "vsa_supervisor_bidi_staging": "agents/supervisor/bidi_app.py",
    "vsa_supervisor_bidi": "agents/supervisor/bidi_app.py",
}

#: Canonical wire name per header group — the camelCase spelling the CLIENT sends and
#: the allowlist carries. Cannot be derived: the code's literals are lowercased
#: (`avxfindingid`), and `AvxFindingId` is not recoverable from that. So this map is
#: explicit, small, and test-enforced — `test_every_read_group_has_a_wire_name` fails
#: when a new header group is added without declaring one, which is what forces the
#: one-line declaration instead of letting a header silently go undeliverable.
#:
#: Consumed by `patch-agentcore-yaml.py`, which reconciles the allowlist at deploy
#: time so the entry does NOT have to be hand-added on every machine. That is the
#: durable fix for this file being gitignored.
_WIRE_NAMES: dict[str, str] = {
    "USER_TOKEN_HEADER": "User-Token",
    "TENANT_QUERY_PARAM": "Tenant-Id",
    "VIN_QUERY_PARAM": "Vin",
    "PERSONA_ID_HEADER": "Persona-Id",
    "CUSTOMER_ID_HEADER": "Customer-Id",
    "VEHICLE_ID_QUERY_PARAMS": "VehicleId",
    "DRIVER_ID_QUERY_PARAMS": "DriverId",
    "AVX_FINDING_ID_QUERY_PARAMS": "AvxFindingId",
    # Declared gaps still get a wire name so the fix is a one-line gap-list
    # deletion rather than two edits in two files.
    "LATITUDE_QUERY_PARAMS": "Latitude",
    "LONGITUDE_QUERY_PARAMS": "Longitude",
}

#: Header groups the code reads that are deliberately NOT allowlisted.
#: constant name -> reason. Shrink this when one is fixed; the covered-but-declared
#: check below fails until you do, so it cannot silently go stale.
_KNOWN_GAPS: dict[str, str] = {
    "LATITUDE_QUERY_PARAMS": (
        "Read at bidi_app.py to skip the /vehicles/{id}/live-state roundtrip "
        "(~150-300ms per session.py's doc comment), but absent from every allowlist "
        "block, so the optimization has silently never fired. Adding it ENABLES a "
        "dormant code path on the latency-critical session-establish surface, which is "
        "a behaviour change owing its own verification rather than a ride-along on an "
        "unrelated commit. Found 2026-09-23 during AVX Fix Group 18; reported to the "
        "operator, deliberately not fixed there."
    ),
    "LONGITUDE_QUERY_PARAMS": "Same as LATITUDE_QUERY_PARAMS — the pair is meaningless split.",
}


class CheckError(RuntimeError):
    """Cannot perform the check — distinct from 'the check failed'."""


def _load_yaml(path: pathlib.Path) -> dict:
    try:
        import yaml  # noqa: PLC0415 — optional at import time so --help works without it
    except ImportError as exc:  # pragma: no cover
        raise CheckError(
            "PyYAML is required. `pip install pyyaml`, or run this inside .venv."
        ) from exc
    if not path.exists():
        raise CheckError(
            f"{path.name} not found at {path}.\n"
            "It is gitignored and per-developer — run `agentcore configure` first."
        )
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise CheckError(f"{path.name} is not valid YAML: {exc}") from exc


def derive_read_headers(entrypoint: pathlib.Path) -> dict[str, list[str]]:
    """Return {constant name: [accepted lowercase casings]} read by *entrypoint*.

    Handles both shapes present in the code: a bare string constant
    (``USER_TOKEN_HEADER = "...user-token"``) and a tuple of accepted casings
    (``VEHICLE_ID_QUERY_PARAMS = ("...vehicleid", "...vehicle-id", "...vehicle_id")``).

    Module level only. A header name computed at runtime or built by concatenation is
    NOT detected — an acknowledged limit, stated rather than left for someone to
    discover, and adequate because every current constant is a plain literal.
    """
    if not entrypoint.exists():
        raise CheckError(f"entrypoint not found: {entrypoint}")
    try:
        tree = ast.parse(entrypoint.read_text())
    except SyntaxError as exc:
        raise CheckError(f"cannot parse {entrypoint}: {exc}") from exc

    found: dict[str, list[str]] = {}
    for node in tree.body:  # module level only — deliberate
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue

        values: list[str] = []
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            values = [node.value.value]
        elif isinstance(node.value, (ast.Tuple, ast.List)):
            values = [
                el.value
                for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]

        casings = [v.lower() for v in values if v.lower().startswith(PREFIX)]
        if casings:
            found[target.id] = casings
    return found


def allowlist_for(config: dict, runtime: str) -> list[str]:
    agents = config.get("agents") or {}
    if runtime not in agents:
        raise CheckError(
            f"runtime {runtime!r} not in {CONFIG.name}. Present: {sorted(agents)}"
        )
    rhc = (agents[runtime] or {}).get("request_header_configuration")
    if rhc is None:
        raise CheckError(
            f"{runtime!r} has `request_header_configuration: null`.\n"
            "Custom headers are documented for WebSocket runtimes only; a text/HTTP "
            "runtime cannot receive them and must smuggle context in the payload."
        )
    return list(rhc.get("requestHeaderAllowlist") or [])


def check(runtime: str) -> dict:
    config = _load_yaml(CONFIG)
    allowed_raw = allowlist_for(config, runtime)
    allowed = {a.lower() for a in allowed_raw}

    entrypoint = REPO / _ENTRYPOINTS[runtime]
    read = derive_read_headers(entrypoint)

    covered: list[str] = []
    uncovered: list[str] = []
    for name, casings in sorted(read.items()):
        (covered if any(c in allowed for c in casings) else uncovered).append(name)

    undeclared = [n for n in uncovered if n not in _KNOWN_GAPS]
    declared = [n for n in uncovered if n in _KNOWN_GAPS]
    # A gap that is now covered must be removed from the list, or the list stops
    # describing reality and the next reader trusts a stale exemption.
    stale = [n for n in covered if n in _KNOWN_GAPS]

    return {
        "runtime": runtime,
        "entrypoint": _ENTRYPOINTS[runtime],
        "allowlist_entries": sorted(allowed_raw),
        "read_groups": {k: v for k, v in sorted(read.items())},
        "covered": covered,
        "declared_gaps": declared,
        "undeclared_gaps": undeclared,
        "stale_gap_declarations": stale,
        "ok": not undeclared and not stale,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runtime", default="vsa_supervisor_bidi_staging",
                    choices=sorted(_ENTRYPOINTS))
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    try:
        result = check(args.runtime)
    except CheckError as exc:
        # Exit 2, never 0: "could not check" must not read as "fine".
        print(f"CANNOT CHECK: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    print(f"runtime   : {result['runtime']}")
    print(f"entrypoint: {result['entrypoint']}")
    print(f"allowlist : {len(result['allowlist_entries'])} entries")
    print(f"code reads: {len(result['read_groups'])} header group(s)")
    print(f"covered   : {len(result['covered'])}")

    for name in result["declared_gaps"]:
        print(f"\nKNOWN GAP  {name}\n  {_KNOWN_GAPS[name]}")

    if result["stale_gap_declarations"]:
        print("\nFAIL — these are now allowlisted but still declared as known gaps:")
        for name in result["stale_gap_declarations"]:
            print(f"  {name}  -> remove it from _KNOWN_GAPS in {pathlib.Path(__file__).name}")

    if result["undeclared_gaps"]:
        print("\nFAIL — the runtime READS these headers but they are NOT allowlisted,")
        print("so AgentCore will drop them silently and the code will see None:")
        for name in result["undeclared_gaps"]:
            casings = result["read_groups"][name]
            wire = casings[0][len(PREFIX):]
            print(f"  {name}")
            print(f"    accepted casings : {casings}")
            print(f"    add to {CONFIG.name} under")
            print(f"      agents.{result['runtime']}.request_header_configuration.requestHeaderAllowlist")
            print(f"    as e.g. X-Amzn-Bedrock-AgentCore-Runtime-Custom-{wire}")
        print("\nThat file is gitignored and per-developer, so this must be done on")
        print("every machine and in every deploy path. See docs/tech.md")
        print('§ "AgentCore custom-header allowlist".')

    print("\nOK" if result["ok"] else "\nVIOLATION")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
