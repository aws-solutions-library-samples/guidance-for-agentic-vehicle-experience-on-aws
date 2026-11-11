"""Shared VIN sanitisation helper for the health-source package.

All three ADP health sources (tire, battery, brake) build Athena SQL with a
VIN literal.  Athena does not support parameterised bindings (no ``?``
placeholders in ``start_query_execution``), so the VIN must be embedded as a
string literal — which makes sanitisation the only available guard.

This module centralises that guard so the three sources share one
allow-list rather than three divergent implementations.  Previously:

- ``adp_tire_source``: allow-listed ``[A-Za-z0-9-]``, raised ``ValueError``
  on empty result  ← correct baseline
- ``adp_brake_source``: escaped single-quotes only (``vin.replace("'", "''")``)
  ← weaker; letters and digits are the VIN alphabet so single-quotes can't
  appear in a valid VIN anyway, but other metacharacters (``-``, ``#``, ``*``)
  passed through
- ``adp_battery_source``: no sanitisation at all  ← missing

Uniform approach (adopted by all three sources as of Fix Group 4b T4.11):

1. Keep only the characters that appear in the VIN standard:
   ``A-Z  0-9  -`` (uppercase alpha-numeric plus the optional hyphen
   placeholder used in some registry conventions).
2. Lower-case input is silently up-cased so ``1fmcu0f70mua12345`` →
   ``1FMCU0F70MUA12345`` — the only normalisation the function performs.
3. If no allowed character survives, raise ``ValueError`` with a message
   that does NOT include the offending VIN (security-review Cycle 7 W4 /
   Cycle 2/3 precedent: exception messages propagate to CloudWatch via
   Lambda's default traceback logger; the VIN is customer-identifying).

Non-exploitable today
---------------------
VINs in S1 come from a CVX-controlled fleet list (``fetch_vehicle_list`` in
the runner), not from user input, so injection via this path is not currently
reachable.  This guard exists because:
  (a) this is a reference architecture — customers will adapt it against their
      own data sources where VINs may come from untrusted callers;
  (b) a defence-in-depth inconsistency between three files in the same
      directory is a shipped pattern that gets copied (security-review Cycle 7
      Warning W7).

See ``.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/security-review.md``
§ Cycle 7 Warning W7 for the full finding.
"""

from __future__ import annotations

__all__ = ["sanitise_vin"]

#: Characters allowed in a VIN literal embedded in Athena SQL.
#: Upper- and lower-case alpha, digits, and hyphen (placeholder convention).
_ALLOWED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")


def sanitise_vin(vin: str) -> str:
    """Return an Athena-safe VIN string, or raise ``ValueError``.

    Keeps only characters in the VIN alphabet (``[A-Za-z0-9-]``).
    Silently up-cases lower-case letters.  If no allowed character remains
    after filtering, raises ``ValueError`` — WITHOUT including the
    offending VIN in the message (security-review Cycle 7 W4).

    Parameters
    ----------
    vin:
        Raw VIN string as received from the fleet list or test fixture.

    Returns
    -------
    str
        Sanitised, upper-cased VIN safe for embedding in an Athena SQL
        ``WHERE vin = '<return-value>'`` literal.

    Raises
    ------
    ValueError
        If no allowed character survives sanitisation.  The message does
        not include the input value to avoid leaking customer data into
        exception messages that propagate to CloudWatch logs.
    """
    sanitised = "".join(c.upper() if c.lower() in "abcdefghijklmnopqrstuvwxyz" else c
                        for c in vin if c in _ALLOWED)
    if not sanitised:
        # Do NOT include the VIN value in this message.  Exception messages
        # propagate to CloudWatch via Lambda's default traceback logger even
        # at default log level.  Security-review Cycle 7 W4 / Cycle 2/3
        # BudgetExceeded repr-leak precedent.
        raise ValueError("VIN contains no valid characters after sanitisation")
    return sanitised
