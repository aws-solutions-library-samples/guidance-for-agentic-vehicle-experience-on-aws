"""Tests for _extract_actor_id — JWT sub resolution, fail-closed (spec T2.4 / D1).

Four cases:
  1. Present sub   — returns the sub string, enabling memory for that turn.
  2. Absent JWT    — returns None; memory is disabled for that turn.
  3. Malformed JWT — returns None for: not 3 segments, bad base64, payload not JSON.
  4. No sub claim  — returns None even when the JWT is otherwise valid.

The mutation test (see comments) confirms that replacing the None fallback
with session_id makes the test suite FAIL — which is the cross-user read
that spec D1 forbids.  The production restore point is always ``return None``.

Run: pytest agents/supervisor/tests/test_actor_id.py -v
"""

from __future__ import annotations

import base64
import json

import pytest

from agents.supervisor.agentcore_app import _extract_actor_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(payload_dict: dict) -> str:
    """Build a three-segment JWT with an unsigned fake signature.

    The runtime never verifies the signature (see _extract_actor_id docstring
    and decisions.md), so a synthetic third segment is sufficient for these
    tests.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_b = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesignature").rstrip(b"=").decode()
    return f"{header}.{payload_b}.{sig}"


# ---------------------------------------------------------------------------
# Case 1 — sub present: actor resolved
# ---------------------------------------------------------------------------

class TestSubPresent:
    """JWT carries a sub claim — actor_id is that sub value."""

    def test_returns_sub_when_present(self):
        jwt = _make_jwt({"sub": "user-abc-123", "exp": 9_999_999_999})
        assert _extract_actor_id(jwt) == "user-abc-123"

    def test_strips_surrounding_whitespace(self):
        """sub values with padding whitespace (edge case) are trimmed."""
        jwt = _make_jwt({"sub": "  user-abc-123  ", "exp": 9_999_999_999})
        assert _extract_actor_id(jwt) == "user-abc-123"

    def test_does_not_log_sub_value(self, caplog):
        """The sub value must never appear in log output (no-PII rule)."""
        import logging
        jwt = _make_jwt({"sub": "SENSITIVE-SUB-VALUE", "exp": 9_999_999_999})
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.agentcore_app"):
            _extract_actor_id(jwt)
        for record in caplog.records:
            assert "SENSITIVE-SUB-VALUE" not in record.getMessage(), (
                "sub value must not be logged"
            )


# ---------------------------------------------------------------------------
# Case 2 — absent JWT: memory disabled
# ---------------------------------------------------------------------------

class TestAbsentJwt:
    """No JWT at all means no actor and memory is off for that turn."""

    def test_none_returns_none(self):
        assert _extract_actor_id(None) is None  # type: ignore[arg-type]

    def test_empty_string_returns_none(self):
        assert _extract_actor_id("") is None

    def test_whitespace_only_returns_none(self):
        assert _extract_actor_id("   ") is None

    def test_non_string_returns_none(self):
        assert _extract_actor_id(12345) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Case 3 — malformed JWT: memory disabled
# ---------------------------------------------------------------------------

class TestMalformedJwt:
    """Any JWT that cannot be parsed safely returns None — no partial reads."""

    def test_not_three_segments_returns_none(self):
        assert _extract_actor_id("onlyone") is None
        assert _extract_actor_id("two.parts") is None
        assert _extract_actor_id("four.parts.make.one.too.many") is None

    def test_bad_base64_returns_none(self):
        # payload segment contains characters outside the base64 alphabet
        assert _extract_actor_id("header.!!!notbase64!!!.sig") is None

    def test_payload_not_json_returns_none(self):
        # valid base64 but not JSON
        garbage = base64.urlsafe_b64encode(b"this is not json").rstrip(b"=").decode()
        assert _extract_actor_id(f"hdr.{garbage}.sig") is None

    def test_payload_json_not_object_returns_none(self):
        # valid JSON but not a dict
        array_b = base64.urlsafe_b64encode(b"[1, 2, 3]").rstrip(b"=").decode()
        jwt = f"hdr.{array_b}.sig"
        # Should not raise; dict.get() would fail on a list, so must be caught
        result = _extract_actor_id(jwt)
        assert result is None


# ---------------------------------------------------------------------------
# Case 4 — well-formed JWT but no sub claim: memory disabled
# ---------------------------------------------------------------------------

class TestNoSubClaim:
    """A syntactically valid JWT without a sub field is fail-closed."""

    def test_missing_sub_returns_none(self):
        jwt = _make_jwt({"exp": 9_999_999_999, "iss": "https://cognito-idp.us-west-2.amazonaws.com/pool"})
        assert _extract_actor_id(jwt) is None

    def test_sub_none_returns_none(self):
        """JSON null sub is treated as missing."""
        jwt = _make_jwt({"sub": None, "exp": 9_999_999_999})
        assert _extract_actor_id(jwt) is None

    def test_sub_empty_string_returns_none(self):
        """An empty sub is not a usable actor — same as absent."""
        jwt = _make_jwt({"sub": "", "exp": 9_999_999_999})
        assert _extract_actor_id(jwt) is None

    def test_sub_whitespace_only_returns_none(self):
        jwt = _make_jwt({"sub": "   ", "exp": 9_999_999_999})
        assert _extract_actor_id(jwt) is None


# ---------------------------------------------------------------------------
# Mutation guard — the cross-user read (D1's fail-closed property)
#
# When the mutation is applied (returning session_id instead of None on
# failure), these tests MUST fail.  If any stay green against the mutation,
# they assert presence, not the property.
#
# Mutation that was applied and confirmed to fail this suite:
#   In _extract_actor_id, replace every `return None` with
#   `return "session-fallback-id"` (simulating a session_id fallback).
# Result: test_none_returns_none, test_empty_string_returns_none,
#         test_missing_sub_returns_none, and all other None-asserting tests
#         FAILED — the mutation was caught.  Restored.
# ---------------------------------------------------------------------------

class TestFailClosedProperty:
    """Explicitly assert the fail-closed contract for every no-sub path.

    These are the tests that catch the session_id fallback mutation.
    They assert the *value* is None (not truthy, not some other string),
    which is what makes them mutation-sensitive.
    """

    @pytest.mark.parametrize("bad_jwt", [
        None,                           # absent
        "",                             # empty
        "   ",                          # whitespace
        "notajwt",                      # one segment
        "two.parts",                    # two segments
        "header.!!!bad_b64!!!.sig",     # invalid base64
    ])
    def test_unusable_jwt_returns_exactly_none(self, bad_jwt):
        """None means 'disable memory'; any other value is a cross-user read."""
        result = _extract_actor_id(bad_jwt)  # type: ignore[arg-type]
        assert result is None, (
            f"Expected None (memory disabled) but got {result!r} — "
            "a non-None fallback on a bad JWT is a cross-user read"
        )

    def test_valid_jwt_no_sub_returns_exactly_none(self):
        jwt = _make_jwt({"exp": 9_999_999_999})
        result = _extract_actor_id(jwt)
        assert result is None, (
            f"Expected None (memory disabled) but got {result!r} — "
            "a non-None fallback when sub is absent is a cross-user read"
        )

    def test_return_type_is_str_or_none(self):
        """The function contract: str on success, None on failure — never another type."""
        jwt_good = _make_jwt({"sub": "uid-999"})
        jwt_bad = _make_jwt({"exp": 9_999_999_999})
        good_result = _extract_actor_id(jwt_good)
        bad_result = _extract_actor_id(jwt_bad)
        assert isinstance(good_result, str)
        assert bad_result is None
