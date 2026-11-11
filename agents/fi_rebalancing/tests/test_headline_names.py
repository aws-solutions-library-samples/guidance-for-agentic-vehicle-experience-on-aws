"""Rebalancing headlines name locations and count correctly.

Issue 2026-11-04-rebalancing-headline-location-ids: a move to a "Car-Share Hub" put the
full-name headline over 120 characters, so it fell back to location ids
("Move 10 vehicles from loc-aus-downtown to loc-aus-hub2: ..."), and a count of 1 read
"1 vehicles". Names now hold until no named form fits; a count of 1 is singular; and a
wording change reaches standing rows because the headline format is part of inputs_hash.
"""
from __future__ import annotations

import itertools
import re

import pytest

from agents.fi_rebalancing import write as w
from agents.fi_rebalancing.evidence import compute_inputs_hash
from agents.fi_rebalancing.tests.test_write import (
    _AGENT_VERSION,
    _CANDIDATE,
    _COMPUTED_AT,
    _FLEET_ID,
    _FROM_LOCATION,
    _GAP_ITEM,
    _ISO_WEEK,
    _PLAN,
    _SURPLUS_GAP_ITEM,
    _TO_LOCATION,
    _make_move_evidence,
    _validate,
)

# Location names as the CMS fleet-locations table holds them on staging (2026-11-04).
_METROS = ("Atlanta", "Austin", "Boston", "Charlotte", "Chicago", "Denver", "Miami",
           "Nashville", "Phoenix", "Portland", "Sacramento", "Seattle")
_KINDS = ("Downtown", "Airport", "Car-Share Hub 1", "Car-Share Hub 2", "Car-Share Hub 3")
# Longer than any staging name (26): only these reach form 3.
_LONG_A = "North Sacramento Car-Share Hub 1"
_LONG_B = "South Sacramento Car-Share Hub 2"


def _move(count, from_name, to_name, short, day="2026-11-04",
          from_id="loc-aus-downtown", to_id="loc-aus-hub2"):
    return w._move_headline(count, from_name, to_name, short, day, from_id, to_id)


def _vins(n):
    return [f"MRDVEH{i:011d}" for i in range(n)]


def _build_move(count, from_name, to_name, short):
    plan = {**_PLAN, "vehicle_vins": _vins(count)}
    cand = {**_CANDIDATE, "vehicles_short": short}
    return w.build_move_finding(
        plan, cand, fleet_id=_FLEET_ID,
        from_location=dict(_FROM_LOCATION, name=from_name),
        to_location=dict(_TO_LOCATION, name=to_name),
        evidence=_make_move_evidence(), computed_at=_COMPUTED_AT,
        agent_version=_AGENT_VERSION, run_date="2026-10-20",
    )


class TestMoveHeadlineKeepsNames:

    def test_hub_destination_keeps_names_in_the_shorter_sentence(self):
        """The 11-04 defect: the full form is 121 characters, so it must not fall to ids."""
        full = ("Move 10 vehicles from Austin Downtown to Austin Car-Share Hub 2: "
                "Austin Car-Share Hub 2 short 12 vehicles from 2026-11-04")
        assert len(full) > 120  # the case the old code sent to ids
        assert _move(10, "Austin Downtown", "Austin Car-Share Hub 2", 12) == (
            "Move 10 vehicles from Austin Downtown to Austin Car-Share Hub 2: "
            "short 12 vehicles from 2026-11-04"
        )

    def test_full_sentence_is_unchanged_when_it_fits(self):
        """The Austin Airport item that already read well keeps its exact wording."""
        assert _move(10, "Austin Downtown", "Austin Airport", 88) == (
            "Move 10 vehicles from Austin Downtown to Austin Airport: "
            "Austin Airport short 88 vehicles from 2026-11-04"
        )

    def test_full_sentence_at_exactly_120_characters_is_kept(self):
        """The 11-02 Austin Car-Share Hub 3 item was exactly 120 characters and read fine."""
        hl = _move(5, "Austin Downtown", "Austin Car-Share Hub 3", 15, day="2026-11-02")
        assert hl == ("Move 5 vehicles from Austin Downtown to Austin Car-Share Hub 3: "
                      "Austin Car-Share Hub 3 short 15 vehicles from 2026-11-02")
        assert len(hl) == 120

    def test_longer_names_drop_the_day_before_the_names(self):
        """Names longer than staging's (32 characters each): the day goes, the names stay."""
        a, b = _LONG_A, _LONG_B
        assert len(f"Move 100 vehicles from {a} to {b}: short 120 vehicles from 2026-11-04") > 120
        assert _move(100, a, b, 120) == f"Move 100 vehicles from {a} to {b}: short 120 vehicles"

    def test_ids_only_when_no_named_form_fits(self):
        hl = _move(2, "A" * 80, "B" * 80, 3, from_id="loc-x", to_id="loc-y")
        assert hl == "Move 2 vehicles from loc-x to loc-y: loc-y short 3 vehicles from 2026-11-04"

    @pytest.mark.parametrize("metro", _METROS)
    def test_every_staging_location_pair_is_named(self, metro):
        """Every same-metro pair on staging, at three-digit counts, is named and fits."""
        names = [f"{metro} {k}" for k in _KINDS]
        for a, b in itertools.permutations(names, 2):
            for count, short in ((1, 1), (10, 12), (999, 999)):
                hl = _move(count, a, b, short)
                assert len(hl) <= 120, hl
                assert "loc-" not in hl, hl
                assert f"from {a} to {b}" in hl, hl


class TestCountsAreSingularAtOne:

    def test_one_vehicle_moved_and_one_short(self):
        assert _move(1, "Atlanta Downtown", "Atlanta Airport", 1, day="2026-11-07") == (
            "Move 1 vehicle from Atlanta Downtown to Atlanta Airport: "
            "Atlanta Airport short 1 vehicle from 2026-11-07"
        )

    def test_one_vehicle_in_the_shorter_sentence(self):
        """The 11-04 "Move 1 vehicles from loc-atl-downtown to loc-atl-hub1" item."""
        assert _move(1, "Atlanta Downtown", "Atlanta Car-Share Hub 1", 6, day="2026-11-07") == (
            "Move 1 vehicle from Atlanta Downtown to Atlanta Car-Share Hub 1: "
            "short 6 vehicles from 2026-11-07"
        )

    def test_one_vehicle_short_in_the_undated_form(self):
        hl = _move(1, _LONG_A, _LONG_B, 1, day="2026-11-07")
        assert hl == f"Move 1 vehicle from {_LONG_A} to {_LONG_B}: short 1 vehicle"

    def test_deficit_gap_one_short(self):
        assert w._gap_deficit_headline("Miami Airport", 1, "2026-11-04") == (
            "Miami Airport: short 1 vehicle from 2026-11-04, no move recommended"
        )

    def test_deficit_gap_two_short_stays_plural(self):
        assert w._gap_deficit_headline("Miami Airport", 2, "2026-11-04") == (
            "Miami Airport: short 2 vehicles from 2026-11-04, no move recommended"
        )

    def test_surplus_gap_one_spare(self):
        assert w._gap_surplus_headline("Miami Downtown", 1) == (
            "Miami Downtown: 1 spare vehicle, no location in the metro is short"
        )

    def test_surplus_gap_two_spare_stays_plural(self):
        assert w._gap_surplus_headline("Miami Downtown", 2) == (
            "Miami Downtown: 2 spare vehicles, no location in the metro is short"
        )


class TestBuiltFindingCarriesTheNamedHeadline:

    def test_built_move_finding_is_named_and_schema_valid(self):
        finding = _build_move(10, "Austin Downtown", "Austin Car-Share Hub 2", 12)
        assert finding["headline"] == (
            "Move 10 vehicles from Austin Downtown to Austin Car-Share Hub 2: "
            "short 12 vehicles from 2026-10-20"
        )
        _validate(finding)

    def test_built_one_vehicle_move_is_singular(self):
        finding = _build_move(1, "Atlanta Downtown", "Atlanta Car-Share Hub 1", 6)
        assert finding["headline"].startswith("Move 1 vehicle from Atlanta Downtown ")
        _validate(finding)


# inputs_hash values the code at AVX main 95d8a2ea computed for these exact fixtures (the
# live rows on staging carry hashes from that code). A wording change must give every
# standing row a new hash, or ``write_finding_if_changed`` only refreshes its expiry and
# the old ids headline stays on screen until the row's inputs happen to change.
_OLD_MOVE_HASH = "4b790f4658352467798cc0a71b673c156dc0c3992dfd1ab60a504fc8b28e6273"
_OLD_DEFICIT_GAP_HASH = "db659d437d366884f9d87adb23f8dee01e18db9f5221b62b8f6693adc4f96335"
_OLD_SURPLUS_GAP_HASH = "c6026ae87defd1c06a9a379d5854c0fa942446ad5e7171a1f9b4ef6c21e3098c"


def _move_hash():
    sk = f"{_PLAN['from_location_id']}/{_PLAN['to_location_id']}/{_ISO_WEEK}"
    return compute_inputs_hash(w._move_summary(
        plan=_PLAN, candidate=_CANDIDATE, subject_key=sk,
        agent_version=_AGENT_VERSION, run_date="2026-10-20"))


def _gap_hash(item):
    return compute_inputs_hash(w._gap_summary(
        gap_item=item, subject_key=f"{item['location_id']}/{_ISO_WEEK}",
        agent_version=_AGENT_VERSION, run_date="2026-10-20"))


class TestWordingChangeReachesStandingRows:

    def test_move_hash_differs_from_the_old_code(self):
        assert _move_hash() != _OLD_MOVE_HASH

    def test_deficit_gap_hash_differs_from_the_old_code(self):
        assert _gap_hash(_GAP_ITEM) != _OLD_DEFICIT_GAP_HASH

    def test_surplus_gap_hash_differs_from_the_old_code(self):
        assert _gap_hash(_SURPLUS_GAP_ITEM) != _OLD_SURPLUS_GAP_HASH

    def test_hash_still_ignores_the_absolute_deficit_day(self):
        """W6 holds: the format marker adds no daily churn."""
        sk = f"{_PLAN['from_location_id']}/{_PLAN['to_location_id']}/{_ISO_WEEK}"
        a = w._move_summary(plan=_PLAN, candidate={**_CANDIDATE, "first_deficit_day": "2026-10-21"},
                            subject_key=sk, agent_version=_AGENT_VERSION, run_date="2026-10-20")
        b = w._move_summary(plan=_PLAN, candidate={**_CANDIDATE, "first_deficit_day": "2026-10-22"},
                            subject_key=sk, agent_version=_AGENT_VERSION, run_date="2026-10-21")
        assert compute_inputs_hash(a) == compute_inputs_hash(b)

    @pytest.mark.parametrize("item", [_GAP_ITEM, _SURPLUS_GAP_ITEM], ids=["deficit", "surplus"])
    def test_gap_hash_stable_across_consecutive_runs(self, item):
        """Review cycle 1 W2: the gap summary's marker adds no daily churn either."""
        sk = f"{item['location_id']}/{_ISO_WEEK}"
        a = w._gap_summary(gap_item={**item, "first_deficit_day": "2026-10-21"}, subject_key=sk,
                           agent_version=_AGENT_VERSION, run_date="2026-10-20")
        b = w._gap_summary(gap_item={**item, "first_deficit_day": "2026-10-22"}, subject_key=sk,
                           agent_version=_AGENT_VERSION, run_date="2026-10-21")
        assert compute_inputs_hash(a) == compute_inputs_hash(b)


# CMS's Action Queue takes the From and To lines out of the headline with this pattern
# (`parseMoveLocations`, modules/cms_ui/source/frontend/src/components/fleet-intelligence/
# action-queue/ActionQueuePanel.tsx), falling back to the location ids when it doesn't match.
_CMS_MOVE_LOCATIONS = re.compile(r"^Move \d+ vehicles? from (.+?) to (.+?): ")


class TestCmsActionQueueReadsTheNames:

    @pytest.mark.parametrize("count,a,b,short", [
        (10, "Austin Downtown", "Austin Airport", 88),               # form 1
        (10, "Austin Downtown", "Austin Car-Share Hub 2", 12),       # form 2
        (1, "Atlanta Downtown", "Atlanta Car-Share Hub 1", 6),       # form 2, singular
        (100, "North Sacramento Car-Share Hub 1", "South Sacramento Car-Share Hub 2", 120),  # form 3
    ])
    def test_every_named_form_parses_to_the_names(self, count, a, b, short):
        m = _CMS_MOVE_LOCATIONS.match(_move(count, a, b, short))
        assert m is not None and (m.group(1), m.group(2)) == (a, b)

    def test_id_form_parses_to_the_ids(self):
        m = _CMS_MOVE_LOCATIONS.match(_move(2, "A" * 80, "B" * 80, 3, from_id="loc-x", to_id="loc-y"))
        assert m is not None and (m.group(1), m.group(2)) == ("loc-x", "loc-y")


# sha256 of the sample headlines below, per ``_HEADLINE_FORMAT``. If this test fails, the
# wording changed: bump ``write._HEADLINE_FORMAT`` (so standing rows are rewritten) and add
# the new digest under the new number. Never edit an existing entry.
_FORMAT_DIGESTS = {
    2: "0c9cea1fcc047627ea21adacd8e115532bb984c56d28acff8c2ca5b8c82155ca",
}


def _format_samples() -> str:
    lines = []
    for count, short in ((1, 1), (5, 15), (10, 12), (100, 120)):
        for a, b in (("Austin Downtown", "Austin Airport"),
                     ("Austin Downtown", "Austin Car-Share Hub 2"),
                     ("Sacramento Car-Share Hub 1", "Sacramento Car-Share Hub 2"),
                     (_LONG_A, _LONG_B),
                     ("A" * 80, "B" * 80)):
            lines.append(_move(count, a, b, short, from_id="loc-a", to_id="loc-b"))
        lines.append(w._gap_deficit_headline("Miami Airport", short, "2026-11-04"))
        lines.append(w._gap_surplus_headline("Miami Downtown", short))
    return "\n".join(lines)


def test_wording_change_bumps_the_headline_format():
    import hashlib
    digest = hashlib.sha256(_format_samples().encode()).hexdigest()
    assert _FORMAT_DIGESTS.get(w._HEADLINE_FORMAT) == digest, (
        f"headline wording or format number changed (format {w._HEADLINE_FORMAT}, digest "
        f"{digest}): bump _HEADLINE_FORMAT and add the digest under the new number"
    )
