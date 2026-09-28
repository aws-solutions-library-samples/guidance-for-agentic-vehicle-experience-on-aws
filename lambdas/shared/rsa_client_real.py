"""Real RSA provider stub — proves the seam, not the integration.

This module exists to make the provider seam structurally complete (Task 1.2
Accept 3): import works, the Protocol is satisfied, and type-checkers are
satisfied. The actual integration is not implemented because both candidate
vendors (Agero, Urgently) are B2B networks requiring a signed partner
agreement before API documentation is accessible.

What a real integration would need:
- Partner credentials (API key or OAuth2 client credentials) for the chosen vendor
- Per-tenant routing: each OEM fleet may have a different roadside program
  and account number; the caller's tenant_id must resolve to vendor-side
  account credentials
- Vehicle attributes beyond VIN (year/make/model, color, license plate)
  that many vendors require to dispatch the right service truck
- Policy/program validation at dispatch time (the coverage check this spec's
  is_covered() stub leaves as a gap — see issues/2026-09-23-rsa-coverage-model-does-not-exist/)
- A webhook or polling strategy for status progression (DISPATCHED →
  EN_ROUTE → ARRIVED), which Group 3.2 simulates deterministically

Activated when VSA_RSA_CLIENT=real.
"""
from __future__ import annotations

from .rsa_client import DispatchAck, DispatchStatus


class RealRsaProvider:
    """Real RSA provider — raises NotImplementedError on every call.

    Exists to prove the seam: importing this module is safe, instantiating
    the class is safe, but any call will fail loudly rather than silently.
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
        raise NotImplementedError(
            "RealRsaProvider is not yet implemented. A real integration requires "
            "partner credentials (Agero or Urgently), per-tenant account routing, "
            "and vehicle attribute enrichment beyond VIN. "
            "See issues/2026-09-23-rsa-coverage-model-does-not-exist/ for context."
        )

    def get_status(self, provider_ref: str) -> DispatchStatus:
        raise NotImplementedError(
            "RealRsaProvider.get_status is not yet implemented. "
            "See issues/2026-09-23-rsa-coverage-model-does-not-exist/ for context."
        )

    def cancel(self, provider_ref: str) -> None:
        raise NotImplementedError(
            "RealRsaProvider.cancel is not yet implemented. "
            "See issues/2026-09-23-rsa-coverage-model-does-not-exist/ for context."
        )
