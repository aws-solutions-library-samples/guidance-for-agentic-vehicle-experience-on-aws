"""Guardrail enforcement for the VSA supervisor.

This package provides:
  - KioskGuardrailEnforcer: synchronous, enforcing ApplyGuardrail check for the
    text path (agentcore_app.py). Deny-on-service-failure — a guardrail outage
    returns the blocked message rather than passing through unchecked.
  - KIOSK_GUARDRAIL_POLICY: the Bedrock Guardrail policy definition as Python
    constants. Imported by vsa-core-stack.ts's counterpart CDK construct and by
    tests; authoritative source of record for what topics/filters are in effect.

Voice / bidi path: NOT changed here. GuardrailChecker in bidi_app.py is a
dry-run observer and remains so — per spec D6, flipping it to enforcing is a
separate decision with its own regression surface.
"""

from agents.supervisor.guardrails.kiosk_guardrail import (
    KioskGuardrailEnforcer,
    KIOSK_GUARDRAIL_NAME,
    KIOSK_GUARDRAIL_TOPICS,
    GUARDRAIL_BLOCKED_INPUT_MESSAGE,
    GUARDRAIL_BLOCKED_OUTPUT_MESSAGE,
)

__all__ = [
    "KioskGuardrailEnforcer",
    "KIOSK_GUARDRAIL_NAME",
    "KIOSK_GUARDRAIL_TOPICS",
    "GUARDRAIL_BLOCKED_INPUT_MESSAGE",
    "GUARDRAIL_BLOCKED_OUTPUT_MESSAGE",
]
