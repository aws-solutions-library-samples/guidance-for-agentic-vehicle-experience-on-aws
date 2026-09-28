"""Tests for customer-stated location in `find_service_center`.

Context: `issues/2026-08-18-oem-test-ride-no-authorised-dealer/`.

Before this, the tool had no location parameter and its docstring instructed the
model NOT to pass one, so a customer saying "I'm in Nashville" was answered from
the VEHICLE's coordinates. The failure mode was not an error — the addresses
returned were real, just for a city the customer was not in, which is worse than
a refusal because nothing signals it.

These tests pin three properties:
  1. A stated city that the network serves becomes the ranking origin.
  2. A stated city it does NOT serve never silently becomes another city's
     dealers presented as the answer — the caller is told.
  3. Absent a stated city, behaviour is exactly as before (vehicle position).

Run:
    pytest agents/supervisor/tests/test_service_network_location.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.supervisor.session import SessionContext
from agents.supervisor.tools.service_network import (
    _resolve_stated_location,
    make_service_network_tool,
)


# ---------------------------------------------------------------------------
# Fixture network — three cities, deliberately far apart so a mis-ranked
# result is unmistakable rather than a rounding argument.
# ---------------------------------------------------------------------------

def _dealer(cid, name, city, state, lat, lon):
    return {
        "centerId": cid, "name": name, "type": "dealer",
        "address": "1 Main Street", "city": city, "state": state,
        "zipCode": "00000", "phone": "+1 555 0100",
        "latitude": lat, "longitude": lon,
        "capabilities": ["test-ride", "scheduled-service"],
        "brandsServiced": ["Meridian"],
        "hours": {"weekday": "9-7"}, "rating": 4.5, "averageWaitDays": 0,
    }


_NETWORK = [
    _dealer("SC-BNA-201", "Meridian of Nashville", "Nashville", "TN", 36.1580, -86.7900),
    _dealer("SC-BNA-202", "Meridian of Brentwood", "Nashville", "TN", 36.0331, -86.7828),
    _dealer("SC-ATL-201", "Meridian of Buckhead", "Atlanta", "GA", 33.8484, -84.3781),
    _dealer("SC-LAS-201", "Meridian Las Vegas Strip", "Las Vegas", "NV", 36.1147, -115.1728),
]

# The vehicle is in Atlanta — as VEH-MRDN-0015 is, per seed-persona-users.py.
_ATLANTA = (33.7488, -84.3877)


@pytest.fixture
def ctx() -> SessionContext:
    c = SessionContext(
        tenant_id="meridian",
        session_id="vsa-sess-stated-location",
        jwt="fake-jwt-not-used",
        persona_id="consumer",
        vin="1HGBH41JXMN000046",
    )
    c.tenant_segment = "oem"
    c.vehicle_id = "VEH-MRDN-0015"
    c.vehicle_make = "Meridian"
    c.client_latitude, c.client_longitude = _ATLANTA
    c.distance_unit = "mi"
    return c


def _call(ctx, **kwargs):
    tool = make_service_network_tool(ctx)
    fn = getattr(tool, "func", tool)   # unwrap the @tool decorator
    with patch("boto3.resource") as res:
        res.return_value.Table.return_value.scan.return_value = {"Items": _NETWORK}
        return fn(**kwargs)


# ---------------------------------------------------------------------------
# 1. A served city becomes the ranking origin
# ---------------------------------------------------------------------------

class TestStatedCityWins:

    def test_nashville_returns_nashville_first(self, ctx):
        """The reported requirement: say Nashville, get Nashville."""
        out = _call(ctx, capability="test ride", vehicle_make="Meridian",
                    near_location="Nashville")
        assert out["found"] is True
        assert "Nashville" in out["centers"][0]["address"], (
            f"expected a Nashville dealer first, got {out['centers'][0]['name']} "
            f"at {out['centers'][0]['address']}"
        )
        assert out["statedLocationMatched"] is True
        assert out["statedLocation"] == "Nashville"
        assert out["locationSource"] == "stated"

    def test_conversational_phrasing_is_accepted(self, ctx):
        """The model passes through what the customer said, not a clean token."""
        for phrasing in ("I'm in Nashville", "nashville", "Nashville, TN",
                         "i'll be in nashville tomorrow"):
            out = _call(ctx, capability="test ride", near_location=phrasing)
            assert "Nashville" in out["centers"][0]["address"], f"failed on {phrasing!r}"

    def test_distance_is_measured_from_the_stated_city(self, ctx):
        """A distance attributed to the customer must be from where THEY are.

        The vehicle is in Atlanta; the Nashville dealer is ~210 mi from it. If
        the origin were still the vehicle, this would report ~210.
        """
        out = _call(ctx, capability="test ride", near_location="Nashville")
        top = out["centers"][0]
        assert top["distance"] < 15, (
            f"{top['name']} reported {top['distance']} mi — ranked from the "
            "vehicle, not from the stated city"
        )

    def test_las_vegas_reaches_the_venue_city(self, ctx):
        out = _call(ctx, capability="test ride", near_location="Las Vegas")
        assert "Las Vegas" in out["centers"][0]["address"]
        assert out["centers"][0]["distance"] < 15


# ---------------------------------------------------------------------------
# 2. An unserved city is never silently swapped for another
# ---------------------------------------------------------------------------

class TestUnservedCityIsReported:

    def test_unserved_city_is_flagged_not_swapped(self, ctx):
        """Honesty property. Handing over Atlanta dealers as the answer to
        "I'm in Boise" is the failure this guards — real addresses, wrong city,
        nothing to signal it."""
        out = _call(ctx, capability="test ride", near_location="Boise")
        assert out["statedLocationMatched"] is False
        assert out["statedLocation"] == "Boise"
        assert "Boise" in out["narration"], (
            "the narration must name the city we cannot serve; otherwise the "
            "customer hears another city's dealer as their answer"
        )

    def test_unserved_city_still_returns_candidates(self, ctx):
        """Useful beats empty — but the narration carries the caveat."""
        out = _call(ctx, capability="test ride", near_location="Boise")
        assert out["found"] is True and out["centers"]

    def test_unserved_city_does_not_claim_the_stated_origin(self, ctx):
        out = _call(ctx, capability="test ride", near_location="Boise")
        assert out.get("locationSource") != "stated"


# ---------------------------------------------------------------------------
# 3. No stated city — unchanged behaviour
# ---------------------------------------------------------------------------

class TestDefaultBehaviourUnchanged:

    def test_absent_stated_location_ranks_from_the_vehicle(self, ctx):
        out = _call(ctx, capability="test ride", vehicle_make="Meridian")
        assert "Atlanta" in out["centers"][0]["address"]
        assert out["statedLocation"] is None
        assert out["statedLocationMatched"] is None

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_blank_stated_location_is_ignored(self, ctx, blank):
        out = _call(ctx, capability="test ride", near_location=blank)
        assert "Atlanta" in out["centers"][0]["address"]
        assert out["statedLocationMatched"] is None

    def test_stated_location_does_not_bypass_brand_or_type_filters(self, ctx):
        """A stated city must not widen the OEM scope.

        Nashville here holds only Meridian dealers, so ask for a brand the
        network does not serve: the answer must be empty, not "here is Nashville
        anyway".
        """
        ctx.vehicle_make = None   # record make wins otherwise, by design
        out = _call(ctx, capability="test ride", vehicle_make="Peugeot",
                    near_location="Nashville")
        assert out["found"] is False


# ---------------------------------------------------------------------------
# Resolver unit tests — pure, no AWS
# ---------------------------------------------------------------------------

class TestResolver:

    def test_exact_and_embedded_matches(self):
        assert _resolve_stated_location(_NETWORK, "Nashville")[2] == "Nashville"
        assert _resolve_stated_location(_NETWORK, "I'm in Las Vegas")[2] == "Las Vegas"

    def test_centroid_is_the_mean_of_that_city(self):
        lat, lon, label = _resolve_stated_location(_NETWORK, "Nashville")
        assert label == "Nashville"
        assert lat == pytest.approx((36.1580 + 36.0331) / 2, abs=1e-6)
        assert lon == pytest.approx((-86.7900 + -86.7828) / 2, abs=1e-6)

    def test_longest_city_wins_so_las_vegas_beats_a_shorter_substring(self):
        net = _NETWORK + [_dealer("X", "X", "Vega", "XX", 1.0, 1.0)]
        assert _resolve_stated_location(net, "Las Vegas")[2] == "Las Vegas"

    @pytest.mark.parametrize("miss", ["Boise", "Tokyo", "", "   ", "somewhere", "ab"])
    def test_no_match_returns_none(self, miss):
        assert _resolve_stated_location(_NETWORK, miss) is None

    def test_non_ascii_city_is_matchable(self):
        """`München` is in the real network. An ASCII-only punctuation strip
        turned it into `m nchen` and reported seven live centers as unserved."""
        net = _NETWORK + [_dealer("M", "Werkstatt", "München", "BY", 48.1, 11.6)]
        assert _resolve_stated_location(net, "München")[2] == "München"
        assert _resolve_stated_location(net, "I'm in München")[2] == "München"

    def test_records_without_coordinates_do_not_crash_the_centroid(self):
        broken = dict(_dealer("B", "No Coords", "Nashville", "TN", 0, 0))
        broken.pop("latitude")
        assert _resolve_stated_location([broken], "Nashville") is None


# ---------------------------------------------------------------------------
# Slot stability across calls
# ---------------------------------------------------------------------------


class TestSlotsAreStableAcrossCalls:
    """The same dealer must offer the same slots to consecutive turns.

    Unseeded `random.choice` re-rolled availability on every invocation, so the
    turn that LISTED "Wednesday at 1 PM" and the turn that BOOKED it saw
    different calendars — the agent offered a slot and then denied it existed
    (observed live 2026-08-19). A conversation is several tool calls; mock data
    that changes between them makes the agent contradict itself.
    """

    def test_repeated_calls_return_identical_slots(self, ctx):
        first = _call(ctx, capability="test ride", near_location="Nashville")
        second = _call(ctx, capability="test ride", near_location="Nashville")
        a = {c["centerId"]: c["nextAvailableSlots"] for c in first["centers"]}
        b = {c["centerId"]: c["nextAvailableSlots"] for c in second["centers"]}
        assert a == b, (
            "slots changed between two identical calls — a booking turn will "
            f"contradict the listing turn.\n  first={a}\n  second={b}"
        )

    def test_slots_are_stable_regardless_of_the_ranking_origin(self, ctx):
        """Same dealer reached two different ways still has one calendar."""
        stated = _call(ctx, capability="test ride", near_location="Nashville")
        vehicle = _call(ctx, capability="test ride", max_results=5)
        by_id = {c["centerId"]: c["nextAvailableSlots"] for c in vehicle["centers"]}
        for c in stated["centers"]:
            if c["centerId"] in by_id:
                assert c["nextAvailableSlots"] == by_id[c["centerId"]], (
                    f"{c['centerId']} offered different slots depending on how it "
                    "was found"
                )

    def test_different_dealers_still_differ(self, ctx):
        """Determinism must not collapse every dealer onto one calendar — then
        the demo shows three dealers with identical availability."""
        out = _call(ctx, capability="test ride", max_results=5)
        seen = [tuple(c["nextAvailableSlots"]) for c in out["centers"]]
        assert len(set(seen)) > 1, (
            "every dealer returned the same slots; the seed is not per-dealer"
        )


# ---------------------------------------------------------------------------
# Unrecognised capability vocabulary
# ---------------------------------------------------------------------------


class TestUnrecognisedCapabilityIsNotADealerGap:
    """An invented capability string must not be reported as "no such dealer".

    The model generates these from conversation. Asked to book a test ride it
    passed `book-appointment`, which exists in no record, so the tool returned
    nothing and the agent told the customer the dealer was not in the system —
    while that dealer sat in the table (observed live 2026-08-19).
    """

    def test_invented_capability_still_returns_dealers(self, ctx):
        out = _call(ctx, capability="book-appointment", near_location="Nashville")
        assert out["found"] is True, (
            "an unrecognised capability must fall back to no capability filter, "
            "not report the dealer network as empty"
        )
        assert "Nashville" in out["centers"][0]["address"]

    @pytest.mark.parametrize("invented", [
        "book-appointment", "booking", "reserve-slot", "schedule-visit", "purchase-enquiry",
    ])
    def test_a_range_of_invented_phrasings_all_degrade_usefully(self, ctx, invented):
        out = _call(ctx, capability=invented, near_location="Nashville")
        assert out["found"] is True, f"{invented!r} produced an empty network"

    def test_segment_and_brand_filters_still_apply(self, ctx):
        """Dropping the capability filter must not widen the OEM scope."""
        ctx.vehicle_make = None
        out = _call(ctx, capability="book-appointment", vehicle_make="Peugeot",
                    near_location="Nashville")
        assert out["found"] is False, (
            "the brand filter must survive the capability fallback"
        )

    def test_a_real_capability_gap_is_still_reported_as_a_gap(self, ctx):
        """The distinction that makes the fallback safe.

        `body-work` IS a real capability elsewhere in the network, so a city
        without a body shop is a genuine gap and must stay empty — otherwise the
        customer is sent to a dealer that cannot do the work.
        """
        network = _NETWORK + [{
            **_dealer("SC-XXX-1", "Some Body Shop", "Atlanta", "GA", 33.7, -84.3),
            "type": "body-shop", "capabilities": ["body-work"],
        }]
        tool = make_service_network_tool(ctx)
        fn = getattr(tool, "func", tool)
        with patch("boto3.resource") as res:
            res.return_value.Table.return_value.scan.return_value = {"Items": network}
            out = fn(capability="body-work", near_location="Nashville")
        assert out["found"] is False
        assert out["capabilityRecognised"] is True, (
            "body-work exists in the network, so this is a real gap and must not "
            "be treated as unrecognised vocabulary"
        )
