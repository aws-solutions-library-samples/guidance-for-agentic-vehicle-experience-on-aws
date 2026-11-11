"""Read the Tier 2 schedules and run-staleness windows out of vsa-tier2-stack.ts.

Shared by test_run_staleness_alarm_pinned.py and test_tier2_staleness_alarms_have_schedules.py
(FI stage C Group 3, fourth review W1, W2, S3). The stack is read as text, not compiled
(this directory runs in the gating CI job; jest runs in none).

Comments are stripped first, so a commented-out entry counts for nothing. The window reader
does not depend on how an entry is written: every quoted name inside the
``runStalenessWindows`` array is a window, plus ``alarmComponents`` when the array spreads it.
Any other identifier in the array (``...fiComponents.map(...)``, a named constant) is a shape
this reader can't resolve, so it fails rather than miss an alarm.
"""
from __future__ import annotations

import pathlib
import re

STACK_PATH = (pathlib.Path(__file__).resolve().parents[3]
              / "infrastructure" / "lib" / "vsa-tier2-stack.ts")

_QUOTED = re.compile(r"""(['"`])([A-Za-z_]\w*)\1""")
_WINDOW_KEYWORDS = frozenset({"component", "emptyHours", "alarmComponents", "map",
                              # task 4.3: new fields added to runStalenessWindows entries
                              "actionsEnabled", "fiRwLive",
                              # task 4.3 B2: fleet-health switch
                              "fiHealthLive",
                              # task 5.2: rebalance switch
                              "fiRebalLive",
                              # JS boolean literals (appear unquoted in actionsEnabled: true)
                              "true", "false"})


def strip_comments(text: str) -> str:
    """Drop // and /* */ comments; keep string literals (so 'https://x' survives)."""
    out: list[str] = []
    i, n, quote = 0, len(text), None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)



def stack_code() -> str:
    """vsa-tier2-stack.ts with its comments removed."""
    return strip_comments(STACK_PATH.read_text())


def _array_body(code: str, name: str) -> str:
    m = re.search(rf"const {name}\b[^=]*=\s*\[(.*?)\];", code, re.S)
    assert m, f"const {name} = [...] not found in vsa-tier2-stack.ts"
    return m.group(1)


def alarm_components(code: str | None = None) -> set[str]:
    """The quoted members of ``alarmComponents`` (nothing else may appear in it)."""
    body = _array_body(code or stack_code(), "alarmComponents")
    rest = _QUOTED.sub("", body)
    assert re.fullmatch(r"[\s,]*", rest), f"alarmComponents is not a list of literals: {body!r}"
    return {name for _, name in _QUOTED.findall(body)}


def staleness_windows(code: str | None = None) -> set[str]:
    """Every component given a run-staleness alarm by ``runStalenessWindows``."""
    code = code or stack_code()
    body = _array_body(code, "runStalenessWindows")
    names = {name for _, name in _QUOTED.findall(body)}
    idents = set(re.findall(r"[A-Za-z_$][\w$]*", _QUOTED.sub("", body))) - _WINDOW_KEYWORDS
    assert not idents, (
        f"runStalenessWindows uses {sorted(idents)}, which this reader can't resolve: "
        "write the entries with quoted component names or extend _tier2_stack_text.py")
    if "alarmComponents" in body:
        names |= alarm_components(code)
    return names


def scheduled_components(code: str | None = None) -> set[str]:
    """Components a Scheduler schedule invokes: the ``componentSchedules`` list plus any
    ``ScheduleTargetInput.fromObject({ ..., component: '<name>' })`` input."""
    code = code or stack_code()
    daily = set(re.findall(r"""component:\s*(['"`])(\w+)\1""",
                           _array_body(code, "componentSchedules")))
    other = set(re.findall(r"""fromObject\(\{[^}]*?\bcomponent:\s*(['"`])(\w+)\1""", code))
    return {name for _, name in daily | other}


def names_quoted_anywhere(component: str, code: str | None = None) -> bool:
    """True if the comment-stripped stack quotes ``component`` anywhere, in any form."""
    return any(name == component for _, name in _QUOTED.findall(code or stack_code()))
