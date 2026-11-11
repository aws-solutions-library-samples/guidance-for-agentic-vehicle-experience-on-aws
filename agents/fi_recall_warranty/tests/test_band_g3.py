"""Group 3 additions to the band table tests (calls G3-9).

Plain tests (not xfail): they pin properties the Group 1 red tests don't reach.
"""
from __future__ import annotations

import pytest

from agents.fi_recall_warranty import band


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_do_not_drive_must_be_a_bool(value):
    """Athena returns strings; "false" is truthy. A non-bool must be refused, not read.

    Control: a real False on the same row classifies normally.
    Mutant: drop the isinstance check -> "false" grounds a POPULATION safety vehicle (p0).
    """
    control = band.classify(recall_class="safety", do_not_drive=False, match_basis="POPULATION")
    assert control.severity == "attention"
    with pytest.raises(ValueError):
        band.classify(recall_class="safety", do_not_drive=value, match_basis="POPULATION")


def test_non_safety_do_not_drive_is_p0_with_grounding():
    """G3-9: a do-not-drive notice is never classified below p0, whatever its class.

    Control: the same non_safety campaign without do_not_drive is row 4/5 (no grounding).
    Mutant: check the class before row 1 -> ValueError (no row) and no grounding item.
    """
    control = band.classify(recall_class="non_safety", do_not_drive=False, match_basis="CONFIRMED")
    assert control.items == ["book"]
    result = band.classify(recall_class="non_safety", do_not_drive=True, match_basis="POPULATION")
    assert result.severity == "p0"
    assert result.items == ["ground", "book"]


def test_items_are_a_fresh_list_each_call():
    """A caller mutating one result must not change the table (writer test N7's premise)."""
    first = band.classify(recall_class="safety", do_not_drive=True, match_basis="CONFIRMED")
    first.items.remove("ground")
    second = band.classify(recall_class="safety", do_not_drive=True, match_basis="CONFIRMED")
    assert second.items == ["ground", "book"]
