"""Shared error types for fleet manager persona tools.

All four fleet tool modules (fleet_health, daily_briefing, fleet_actions,
decision_journal) import from here so the error hierarchy is defined once.

Mirrors the TriageError / TriageAuthError hierarchy in triage.py — one base
class for non-2xx responses, one sub-class for 401 specifically.
"""

from __future__ import annotations


class FleetToolError(RuntimeError):
    """A CMS fleet endpoint returned a non-2xx response."""


class FleetToolAuthError(FleetToolError):
    """A CMS fleet endpoint rejected the request with HTTP 401.

    Most likely cause: the forwarded Cognito IdToken has expired or is
    invalid for the CMS Cognito User Pool. The caller should re-authenticate
    and retry.
    """
