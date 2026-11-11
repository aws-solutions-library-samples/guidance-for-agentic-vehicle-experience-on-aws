"""Tests for agents/health/_vin.py — shared VIN sanitisation helper.

Security-review Cycle 7 Warning W7: SQL-injection defence must be uniform
across all three ADP health sources (tire, battery, brake).  The shared
``sanitise_vin`` function in ``agents/health/_vin.py`` is the single
implementation all three sources delegate to.

The guard is an **allow-list that strips** (not rejects) invalid characters.
It raises ``ValueError`` only when NO valid character survives.  This is the
same behaviour the tire source had originally — a crafted input like
``"1FMCU0F70MUA12345' OR '1'='1"`` is defanged by stripping the single
quotes, spaces, and operator characters, leaving ``"1FMCU0F70MUA12345OR11"``,
which is safe to embed in an Athena SQL literal.

Coverage:
- Valid VINs pass through unchanged (upper-cased)
- Lower-case VINs are silently up-cased
- SQL metacharacters (quotes, spaces, semicolons, %) are stripped
- The stripped result contains no characters that can break an Athena
  SQL string literal
- Empty-after-filtering inputs raise ValueError without the VIN in message
- Hyphen is allowed (legitimate VIN placeholder character)
- Control characters are stripped; if all chars were control, raises ValueError
"""

from __future__ import annotations

import pytest

from agents.health._vin import sanitise_vin


# ---------------------------------------------------------------------------
# Valid inputs
# ---------------------------------------------------------------------------


def test_uppercase_vin_passes_through():
    """A standard uppercase 17-char VIN is returned unchanged."""
    vin = "1FMCU0F70MUA12345"
    assert sanitise_vin(vin) == vin


def test_lowercase_vin_is_upcased():
    """Lower-case letters in the VIN are silently up-cased."""
    assert sanitise_vin("1fmcu0f70mua12345") == "1FMCU0F70MUA12345"


def test_mixed_case_vin_is_upcased():
    """Mixed-case VIN is fully up-cased."""
    assert sanitise_vin("1FmCu0F70mUa12345") == "1FMCU0F70MUA12345"


def test_hyphen_is_allowed():
    """Hyphens are part of the allowed VIN character set."""
    result = sanitise_vin("1FMCU0F70-UA12345")
    assert result == "1FMCU0F70-UA12345"


def test_digits_and_alpha_pass():
    """Pure alphanumeric VIN passes through."""
    assert sanitise_vin("ABCDEFGHJ1234567") == "ABCDEFGHJ1234567"


# ---------------------------------------------------------------------------
# SQL metacharacter stripping (allow-list removes — does not reject on sight)
# ---------------------------------------------------------------------------


def test_single_quote_is_stripped():
    """Single quotes (SQL string delimiter) are removed from the output.

    The shared helper uses allow-list filtering, not escaping.  A crafted
    input containing a single quote has the quote stripped.  The remaining
    valid alphanumeric characters are safe to embed in a SQL literal.
    """
    # A VIN-suffix injection: the quote and space are stripped; valid chars remain
    result = sanitise_vin("1FMCU' OR 1=1--")
    assert "'" not in result, f"Single quote must be stripped; got {result!r}"
    # Valid alphanumeric chars and hyphens survive (1, F, M, C, U, O, R, 1, 1, -, -)
    assert result == "1FMCUOR11--"


def test_semicolon_is_stripped():
    """Semicolons (SQL statement terminator) are removed."""
    result = sanitise_vin("1FMCU0F;")
    assert ";" not in result
    assert result == "1FMCU0F"


def test_space_is_stripped():
    """Spaces are not in the VIN alphabet and are removed."""
    result = sanitise_vin("1FMCU 0F70M")
    assert " " not in result
    assert result == "1FMCU0F70M"


def test_hash_is_stripped():
    """Hash characters (MySQL comment) are removed."""
    result = sanitise_vin("1FMCU0#COMMENT")
    assert "#" not in result
    assert result == "1FMCU0COMMENT"


def test_percent_is_stripped():
    """Percent (SQL LIKE wildcard) is removed."""
    result = sanitise_vin("1FMCU0%F70MUA12345")
    assert "%" not in result
    assert result == "1FMCU0F70MUA12345"


def test_underscore_is_stripped():
    """Underscore (SQL LIKE wildcard) is removed."""
    result = sanitise_vin("1FMCU_0F70MUA12345")
    assert "_" not in result
    assert result == "1FMCU0F70MUA12345"


def test_backslash_is_stripped():
    """Backslash is removed."""
    result = sanitise_vin("1FMCU\\0F70MUA12345")
    assert "\\" not in result
    assert result == "1FMCU0F70MUA12345"


def test_null_byte_is_stripped():
    """Null bytes are stripped; if valid chars remain, no exception."""
    # The null byte is stripped; valid chars remain (1, F, M, C, U, ...)
    result = sanitise_vin("1FMCU\x000F70MUA12345")
    assert "\x00" not in result
    assert result == "1FMCU0F70MUA12345"


def test_newline_is_stripped():
    """Newlines are stripped."""
    result = sanitise_vin("1FMCU\n0F70MUA12345")
    assert "\n" not in result
    assert result == "1FMCU0F70MUA12345"


# ---------------------------------------------------------------------------
# Injection sequence: result contains no SQL-breaking characters
# ---------------------------------------------------------------------------


def test_vin_with_union_select_injection_is_defanged():
    """A crafted input of the form VIN' UNION SELECT... is defanged.

    The SQL context is: ``WHERE vin = '{result}'``.  A single-quote allows
    breaking out of the string literal.  After sanitisation, the output
    contains no single-quotes, so no SQL escape is possible.
    """
    crafted = "1FMCU0F70MUA12345' UNION SELECT password FROM users--"
    result = sanitise_vin(crafted)

    # The injection structure (quote, space, operator) is completely gone
    assert "'" not in result, "No single-quote in sanitised output"
    assert " " not in result, "No space in sanitised output"

    # No SQL-breaking characters remain
    for bad_char in ("'", '"', ";", " ", "\n", "\r", "\t"):
        assert bad_char not in result, (
            f"SQL-breaking character {bad_char!r} survived sanitisation in {result!r}"
        )


def test_sanitised_output_safe_for_sql_embedding():
    """All characters in the sanitised output are safe to embed in a SQL literal.

    Specifically: only [A-Z0-9-] are present; none of these can break an
    Athena SQL string literal delimited with single quotes.
    """
    import re

    test_inputs = [
        "1FMCU0F70MUA12345",
        "1fmcu0f70mua12345",
        "1FMCU0F70MUA12345' OR '1'='1",
        "1FMCU0F70MUA12345; DROP TABLE",
        "1FMCU0F70MUA12345\x00\x01",
    ]
    for vin in test_inputs:
        try:
            result = sanitise_vin(vin)
        except ValueError:
            continue  # empty result — guard correctly rejected
        # The result must match the VIN-safe character set exactly
        assert re.fullmatch(r"[A-Z0-9\-]+", result), (
            f"sanitise_vin({vin!r}) returned {result!r} which contains "
            "characters outside [A-Z0-9-]"
        )


# ---------------------------------------------------------------------------
# ValueError on empty result (without VIN in message)
# ---------------------------------------------------------------------------


def test_all_invalid_chars_raises_value_error():
    """A VIN containing only invalid chars raises ValueError."""
    with pytest.raises(ValueError):
        sanitise_vin("!@#$%^&*()")


def test_empty_string_raises_value_error():
    """An empty VIN raises ValueError."""
    with pytest.raises(ValueError):
        sanitise_vin("")


def test_control_chars_only_raises_value_error():
    """A VIN of only control characters raises ValueError."""
    with pytest.raises(ValueError):
        sanitise_vin("\x00\x01\x02\x03")


def test_value_error_message_does_not_contain_vin():
    """ValueError message must NOT include the offending VIN value.

    Exception messages propagate to CloudWatch via Lambda's default traceback
    logger even at default log level.  Including the VIN would leak customer
    data — same class as the Cycle 2/3 BudgetExceeded repr-leak fix
    (security-review Cycle 7 W4 / W3 precedent).
    """
    bad_vin = "!@#"  # will survive no filtering
    with pytest.raises(ValueError) as exc_info:
        sanitise_vin(bad_vin)
    exc_text = str(exc_info.value)
    assert bad_vin not in exc_text, (
        f"ValueError message must not include the offending VIN. "
        f"Got: {exc_text!r}"
    )


def test_value_error_message_does_not_contain_control_char_vin():
    """ValueError for a control-character VIN must not include repr of the VIN.

    This mirrors test_no_vin_in_sanitise_vin_exception_message in
    test_adp_tire_source.py, extended to the shared helper.
    """
    bad_vin = "\x00\x01"
    with pytest.raises(ValueError) as exc_info:
        sanitise_vin(bad_vin)
    exc_text = str(exc_info.value)
    # repr of control chars like '\x00' must not appear
    assert "\x00" not in exc_text
    assert "\x01" not in exc_text
    assert repr(bad_vin) not in exc_text


# ---------------------------------------------------------------------------
# Shared helper is the single implementation used by all three sources
# ---------------------------------------------------------------------------


def test_tire_source_delegates_to_shared_helper():
    """adp_tire_source._sanitise_vin delegates to agents.health._vin.sanitise_vin.

    This test proves the tire source no longer maintains a separate allow-list
    implementation — it wraps the shared helper.  If _sanitise_vin is
    overridden to use different logic, this test catches the divergence.
    """
    from agents.health.adp_tire_source import _sanitise_vin as tire_sanitise
    from agents.health._vin import sanitise_vin as shared_sanitise

    # Same input must produce identical output from both
    test_vins = [
        "1FMCU0F70MUA12345",       # standard uppercase
        "1fmcu0f70mua12345",       # lowercase → should become uppercase
        "1FMCU0F70MUA12345",       # alphanumeric
    ]
    for vin in test_vins:
        assert tire_sanitise(vin) == shared_sanitise(vin), (
            f"adp_tire_source._sanitise_vin and agents.health._vin.sanitise_vin "
            f"disagree for {vin!r}: {tire_sanitise(vin)!r} vs {shared_sanitise(vin)!r}"
        )


def test_tire_source_sanitise_raises_for_all_metacharacters():
    """adp_tire_source._sanitise_vin raises ValueError for all-metachar input.

    Since the tire source now delegates to the shared helper, this test
    confirms the delegation is complete and not a no-op.
    """
    from agents.health.adp_tire_source import _sanitise_vin

    with pytest.raises(ValueError):
        _sanitise_vin("!@#$%")
