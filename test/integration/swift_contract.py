"""Cross-repo contract readers for Swift-only declarations.

WHY THIS EXISTS
---------------
Some contracts this spec depends on live ONLY in Swift, in the sibling CMS
repo: ``LayoutSegment.showsBuyTab`` / ``buyTabLabel`` / ``buyTabSymbol`` and
the ``AcquireScenario`` Path A / Path B cases. There is no Python symbol to
import — verified 2026-08-04, ``showsBuyTab`` appears nowhere in CVX Python
or JSON.

The first cut of the Group 5 integration tests handled that by building a
dict inside the test and asserting the dict's own values. Review cycle 3
correctly flagged those as tautological: they cannot fail for any real
reason, and they inflated the suite's apparent coverage.

This module reads the **authoritative Swift source** instead, so the
assertions become real cross-repo contract tests that fail when the Swift
drifts. That is worth having: rewriting the first of them immediately
surfaced a false belief — the tautological test asserted
``rental -> buyTabLabel == ""`` while the Swift actually returns ``"Buy"``
(deliberately, with an "unreachable when showsBuyTab is checked first"
comment). A test that asserts its own fixture can never catch that.

SCOPE
-----
These read the CONTRACT, not the rendering. Verifying that SwiftUI actually
draws the tab still needs an XCTest target, which spec
``2026-08-04-zone2-ipad-adaptive-kiosk-and-xctest`` owns — hence the ``[!]``
markers on the rendering halves of those scenarios.

A targeted line scan is deliberate: adding a real Swift parser dependency
would be disproportionate for reading a handful of switch arms.

Sibling-repo discovery follows the established precedent in
``scripts/tests/test_seed_scripts_no_fake_connected.py`` — ``CMS_REPO_PATH``
env var, falling back to ``../connected-mobility-guidance-on-aws/``. When the
sibling repo is absent the caller skips, so the CVX suite stays runnable
standalone.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

CVX_REPO = Path(__file__).resolve().parents[2]


def discover_cms_repo() -> Path | None:
    """Locate the sibling CMS repo, or None when it is not checked out."""
    env = os.environ.get("CMS_REPO_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    candidate = CVX_REPO.parent / "connected-mobility-guidance-on-aws"
    return candidate if candidate.is_dir() else None


CMS_REPO = discover_cms_repo()

_LAYOUT_SEGMENT_REL = Path("clients/ios/VSACompanion/Theming/LayoutSegment.swift")
_ACQUIRE_SCENARIO_REL = Path("clients/ios/VSACompanion/Presenter/AcquireScenario.swift")


def swift_file(rel: Path) -> Path | None:
    """Absolute path to a Swift source file in the sibling repo, if present."""
    if CMS_REPO is None:
        return None
    p = CMS_REPO / rel
    return p if p.is_file() else None


def layout_segment_source() -> Path | None:
    return swift_file(_LAYOUT_SEGMENT_REL)


def acquire_scenario_source() -> Path | None:
    return swift_file(_ACQUIRE_SCENARIO_REL)


def _computed_property_body(text: str, prop: str) -> str:
    """Return the source of a `var <prop>: T { ... }` computed property.

    Brace-counts from the opening brace so nested braces inside the body do
    not terminate the match early.
    """
    m = re.search(rf"\bvar\s+{re.escape(prop)}\s*:\s*[A-Za-z0-9_<>\[\]?]+\s*\{{", text)
    if not m:
        raise AssertionError(f"computed property 'var {prop}' not found in Swift source")
    i = m.end() - 1
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise AssertionError(f"unbalanced braces reading 'var {prop}'")


def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def parse_switch_returns(body: str) -> dict[str, str]:
    """Map each enum case in a `switch self` body to its returned literal.

    Handles both arm layouts, because either is valid Swift and a formatter
    may rewrite one into the other:

        case .oem, .fleet: return true          # single line
        case .oem, .fleet:                      # body on the next line
            return true

    The single-line-only version of this parser returned an EMPTY dict against
    the multi-line form. That is the dangerous failure: the tests consuming it
    then reported a contract violation rather than a parse failure, sending the
    reader to look for drift in Swift that had not changed. Verified 2026-08-05
    by running the real tests against a reformatted-but-identical fixture: two
    false failures. `layout_segment_property` now raises on an empty parse so a
    parser failure can never masquerade as a contract violation again.

    Trailing `//` comments are stripped first so an explanatory note after a
    `return` cannot leak into the parsed value.
    """
    out: dict[str, str] = {}
    lines = _strip_comments(body).splitlines()
    pending: list[str] = []

    def _clean(value: str) -> str:
        value = value.rstrip(";").strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        return value

    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r"case\s+(.+?):\s*(.*)$", line)
        if m:
            cases = [c.strip().lstrip(".") for c in m.group(1).split(",") if c.strip()]
            rest = m.group(2).strip()
            rm = re.match(r"return\s+(.+)$", rest)
            if rm:
                # Single-line arm: `case .oem: return true`
                for c in cases:
                    out[c] = _clean(rm.group(1))
                pending = []
            else:
                # Multi-line arm: remember the cases, take the next `return`.
                pending = cases
            continue
        rm = re.match(r"return\s+(.+)$", line)
        if rm and pending:
            for c in pending:
                out[c] = _clean(rm.group(1))
            pending = []
    return out


def layout_segment_property(prop: str) -> dict[str, str]:
    """Parse one LayoutSegment computed property into {case: returned literal}.

    Raises on an empty parse. An empty result means THIS PARSER failed, not
    that the Swift declares nothing — and a caller asserting on `{}` would
    report a contract violation for Swift that never changed. Failing loudly
    keeps "the parser broke" distinguishable from "the contract drifted",
    which is the whole reason these tests exist.
    """
    src = layout_segment_source()
    assert src is not None, "sibling CMS repo not available"
    parsed = parse_switch_returns(_computed_property_body(src.read_text(), prop))
    assert parsed, (
        f"parsed ZERO switch arms from LayoutSegment.{prop} in {src}. "
        "This is a parser failure, not a contract violation — the Swift layout "
        "probably changed shape (e.g. reformatted). Fix swift_contract.py rather "
        "than the Swift or the assertion."
    )
    return parsed


def layout_segment_cases() -> set[str]:
    """The declared `case` names on the LayoutSegment enum itself."""
    src = layout_segment_source()
    assert src is not None, "sibling CMS repo not available"
    text = _strip_comments(src.read_text())
    m = re.search(r"enum\s+LayoutSegment\b[^{]*\{", text)
    assert m, "enum LayoutSegment declaration not found"
    # Only scan until the first computed property, so `case .oem:` switch arms
    # inside property bodies are not mistaken for enum case declarations.
    tail = text[m.end() :]
    cut = re.search(r"\bvar\s+\w+\s*:", tail)
    head = tail[: cut.start()] if cut else tail
    return {
        c.strip()
        for line in head.splitlines()
        if (mm := re.match(r"\s*case\s+([A-Za-z0-9_,\s]+)$", line))
        for c in mm.group(1).split(",")
        if c.strip()
    }


def acquire_scenario_cases() -> dict[str, str]:
    """Map AcquireScenario case names to their raw String values."""
    src = acquire_scenario_source()
    assert src is not None, "sibling CMS repo not available"
    text = _strip_comments(src.read_text())
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'case\s+(\w+)\s*=\s*"([^"]+)"', text)
    }


def acquire_scenario_declares(symbol: str) -> bool:
    """True when AcquireScenario.swift declares the given symbol."""
    src = acquire_scenario_source()
    assert src is not None, "sibling CMS repo not available"
    return re.search(rf"\b{re.escape(symbol)}\b", _strip_comments(src.read_text())) is not None
