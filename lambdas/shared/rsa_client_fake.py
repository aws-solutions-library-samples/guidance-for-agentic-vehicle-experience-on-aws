"""Fake RSA provider for development and demo use.

Returns deterministic responses — the same inputs produce the same ETA on
every replay, satisfying Task 1.2 Accept 4: "A demo that shows a different
ETA on every replay of the same scenario is not demoable."

Activated when VSA_RSA_CLIENT=fake (the default).
"""
from __future__ import annotations

import hashlib

from .rsa_client import (
    LIFECYCLE_DISPATCHED,
    LIFECYCLE_EN_ROUTE,
    DispatchAck,
    DispatchStatus,
)

# ETA range for the fake: 18–45 minutes, deterministic per vin+service_type.
_ETA_MIN = 18
_ETA_RANGE = 28  # _ETA_MIN + _ETA_RANGE = 46 exclusive upper bound


def _deterministic_eta(vin: str, service_type: str) -> int:
    """Return a deterministic ETA in minutes for the given inputs.

    Uses the first two bytes of a SHA-256 digest of the (vin, service_type)
    pair so the same inputs always produce the same result — seeded, not
    random.random().
    """
    seed = f"{vin}:{service_type}".encode()
    digest = hashlib.sha256(seed).digest()
    return _ETA_MIN + (int.from_bytes(digest[:2], "big") % _ETA_RANGE)


class FakeRsaProvider:
    """In-process fake satisfying the RsaProvider protocol.

    Implements all three protocol methods so tests and type checkers are happy.
    """

    def request_dispatch(
        self,
        vin: str,
        lat: float,
        lon: float,
        service_type: str,
        tenant_id: str,
        callback_contact: str,
        dispatch_id: str,
    ) -> DispatchAck:
        """Return a simulated dispatch acknowledgement.

        provider_ref is prefixed "SIM-" per spec R4 so downstream consumers
        can identify simulated dispatches without relying on the simulated flag.
        provider_ref is derived from a SHA-256 hash of dispatch_id so that:
          - two distinct dispatches never collide (uniqueness, F1.4)
          - same dispatch_id always yields the same ref (determinism)
          - caller input is NOT recoverable from the ref (opacity, F2.2);
            a real vendor's reference is opaque — leaving it invertible would
            invite Group 2/3 consumers to reverse-derive dispatchId from
            providerRef, which breaks on the first real provider.
        eta_minutes is deterministic given (vin, service_type).
        simulated=True is always set per spec R4.
        """
        provider_ref = f"SIM-{hashlib.sha256(dispatch_id.encode()).hexdigest()[:12].upper()}"
        return DispatchAck(
            provider_ref=provider_ref,
            eta_minutes=_deterministic_eta(vin, service_type),
            simulated=True,
            status=LIFECYCLE_DISPATCHED,
        )

    def get_status(self, provider_ref: str) -> DispatchStatus:
        """Return a simulated status for the given provider reference."""
        return DispatchStatus(
            provider_ref=provider_ref,
            status=LIFECYCLE_EN_ROUTE,
            eta_minutes=15,
            simulated=True,
        )

    def cancel(self, provider_ref: str) -> None:
        """No-op cancellation for the fake provider."""
        return None
