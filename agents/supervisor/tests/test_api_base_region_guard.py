"""VSA_API_BASE must not name a different region than the runtime.

On 2026-09-22 a staging deploy baked the CVX **prod** API URL — us-east-1, torn
down 2026-09-02 — into both staging runtimes, because the Makefile prefers an
ambient `VSA_API_BASE` over its own CloudFormation lookup and the operator's
shell carried the value from a stale `.env`. A driver asking for a human agent
was told escalation was unavailable; auto-triage failed DNS resolution.

The guard is in the runtime's config rather than the deploy target because
`--env` replaces the whole runtime environment on redeploy, so a wrong value can
arrive without the Makefile being involved. See
issues/2026-09-22-stale-env-vsa-api-base-leaked-into-staging-runtime/.

Run:
    pytest agents/supervisor/tests/test_api_base_region_guard.py -v
"""

from __future__ import annotations

import pytest

from agents.supervisor.config import (
    _api_base_region,
    assert_api_base_region_matches,
)

# The real value from the incident, and its correct counterpart.
PROD_USE1 = "https://jsy5rf5c2b.execute-api.us-east-1.amazonaws.com/prod"
STAGING_USW2 = "https://xgjh5gq25l.execute-api.us-west-2.amazonaws.com/staging"


# ---------------------------------------------------------------------------
# Region extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "api_base,expected",
    [
        (PROD_USE1, "us-east-1"),
        (STAGING_USW2, "us-west-2"),
        ("https://abc.execute-api.ap-southeast-2.amazonaws.com/staging", "ap-southeast-2"),
        ("https://abc.execute-api.eu-central-1.amazonaws.com/x", "eu-central-1"),
    ],
)
def test_region_is_read_from_an_api_gateway_host(api_base, expected):
    assert _api_base_region(api_base) == expected


@pytest.mark.parametrize(
    "api_base",
    [
        "",
        "   ",
        "https://api.cvx.example.com/staging",   # custom domain — unknowable
        "http://localhost:3000",                 # local dev
        "https://example.com/execute-api/x",     # superficially similar, not a host
        "not-a-url",
        # A custom domain that EMBEDS a region name. Only an `execute-api`
        # host names the region authoritatively; a hostname or path that merely
        # contains a region-shaped string does not. Without this case the
        # extraction could be broadened to match anywhere with the suite still
        # green (mutation R6), and legitimate custom domains would then be
        # rejected on a region they only happen to mention.
        "https://api-us-west-2.cvx.example.com/staging",
        "https://api.cvx.example.com/prod-us-east-1/v1",
    ],
)
def test_undeterminable_regions_return_none_rather_than_guessing(api_base):
    """Conservative by design: the guard must only compare regions it knows."""
    assert _api_base_region(api_base) is None


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def test_the_actual_incident_is_rejected():
    """us-east-1 prod URL against a us-west-2 deploy — the exact 2026-09-22 bug."""
    with pytest.raises(ValueError) as excinfo:
        assert_api_base_region_matches(PROD_USE1, "us-west-2")

    msg = str(excinfo.value)
    # The message must name BOTH regions and the offending value, or the operator
    # is no better off than with the bare class name that started all this.
    assert "us-east-1" in msg
    assert "us-west-2" in msg
    assert PROD_USE1 in msg
    # …and it must name the remedy.
    assert "RestApiUrl" in msg


def test_matching_regions_are_permitted():
    assert_api_base_region_matches(STAGING_USW2, "us-west-2") is None


@pytest.mark.parametrize(
    "api_base,region",
    [
        ("https://api.cvx.example.com/staging", "us-west-2"),  # custom domain
        ("", "us-west-2"),                                      # unset
        (STAGING_USW2, ""),                                     # region unknown
        # A custom domain naming a DIFFERENT region than the deploy must still be
        # permitted — the name is not authoritative, and a guard that rejects a
        # working configuration gets deleted by the next person to hit it.
        ("https://api-us-west-2.cvx.example.com/staging", "eu-central-1"),
    ],
)
def test_the_guard_stays_silent_when_it_cannot_prove_a_mismatch(api_base, region):
    """A guard that fires on the unknowable gets disabled by the next person."""
    assert assert_api_base_region_matches(api_base, region) is None


def test_every_commercial_region_shape_is_matched_not_just_three_part():
    """Region ids vary in shape; a too-strict regex would silently pass a mismatch.

    `us-east-1` is three segments, `ap-southeast-2` is three, `us-gov-west-1` is
    four. If the pattern only handled the common shape, a mismatch in a
    four-segment region would go undetected — which is a silent failure, the
    thing this whole guard exists to prevent.
    """
    assert _api_base_region("https://a.execute-api.us-gov-west-1.amazonaws.com/p") == "us-gov-west-1"
    with pytest.raises(ValueError):
        assert_api_base_region_matches(
            "https://a.execute-api.us-gov-west-1.amazonaws.com/p", "us-west-2"
        )
