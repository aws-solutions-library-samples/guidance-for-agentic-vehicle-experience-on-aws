"""Every Tier 2 run-staleness alarm watches a component something actually invokes.

The staleness alarms treat missing data as BREACHING and notify the operator topic, so an
alarm for a component with no schedule pages every day. FI stage C Group 3 (landing check)
found recall/warranty alarms added before task 4.3 adds their schedules.
Reads infrastructure/lib/vsa-tier2-stack.ts as text through _tier2_stack_text (fourth review
W2: the window reader no longer depends on how an entry is written).
"""
from __future__ import annotations

import pytest

from agents.tier2.tests._tier2_stack_text import (
    alarm_components,
    names_quoted_anywhere,
    scheduled_components,
    staleness_windows,
    strip_comments,
)

#: Today's scheduled components, which are also today's staleness windows. task 4.3
#: added recall and warranty to BOTH sets in the same change. task 4.3 B2 adds
#: ``service`` to BOTH sets; investigation is event-triggered (SQS), not scheduled,
#: and gets no staleness window (task43-notes.md item 2).  task 5.2 adds ``rebalance``
#: to BOTH sets in the same change — the schedule and the staleness alarm land
#: together so the pair of pin tests catch either side being dropped.
_TODAY = {"battery", "tires", "brakes", "cost", "diagnostics", "recall", "warranty",
          "service", "rebalance"}


def test_the_readers_see_exactly_todays_schedules_and_windows():
    """Positive control, an equality (fourth review W2): a reader that misses an entry fails.

    CHANGED by task 4.3 B2: ``service`` added to both sets; ``investigation`` is
    event-triggered and absent from both.
    """
    assert alarm_components() == {"battery", "tires", "brakes"}
    assert scheduled_components() == _TODAY
    assert staleness_windows() == _TODAY


def test_every_staleness_window_has_a_schedule():
    unscheduled = staleness_windows() - scheduled_components()
    assert unscheduled == set(), (
        f"staleness alarms for components nothing invokes: {sorted(unscheduled)}")


def test_the_window_reader_sees_every_entry_form():
    """Fourth review W2 (S4, S5): a spread over a literal list, double quotes and an
    alarmComponents member are all windows; an unresolvable identifier fails the read."""
    code = ("const alarmComponents: string[] = ['battery', 'recall'];\n"
            "const runStalenessWindows: Array<{ component: string; emptyHours: number }> = [\n"
            "  ...alarmComponents.map((component) => ({ component, emptyHours: 27 })),\n"
            "  ...['warranty', 'cost'].map((component) => ({ component, emptyHours: 27 })),\n"
            '  { component: "diagnostics", emptyHours: 2 },\n'
            "  // { component: 'commented', emptyHours: 27 },\n"
            "];\n")
    code = strip_comments(code)
    assert staleness_windows(code) == {"battery", "recall", "warranty", "cost", "diagnostics"}
    unresolvable = code.replace("...['warranty', 'cost']", "...fiComponents")
    with pytest.raises(AssertionError, match="fiComponents"):
        staleness_windows(unresolvable)


def test_comments_are_stripped_and_strings_kept():
    """Fourth review S3: a commented-out schedule or window counts for nothing."""
    text = ("a = 'https://x'; // { component: 'recall', hour: '4' }\n"
            "/* fromObject({ stage, component: 'warranty' }) */ b = \"it's\";\n")
    assert strip_comments(text) == "a = 'https://x'; \n b = \"it's\";\n"



def test_names_quoted_anywhere_sees_a_whole_quoted_name_only():
    """The exemption test's second layer (fourth review W1): any quoted mention counts, a
    comment or a longer string does not; positive control on the real stack."""
    assert names_quoted_anywhere("battery")
    assert names_quoted_anywhere("recall", strip_comments('x = { component: "recall" };'))
    assert not names_quoted_anywhere("recall", strip_comments("// 'recall'\ny = 'vsa-recall';"))
