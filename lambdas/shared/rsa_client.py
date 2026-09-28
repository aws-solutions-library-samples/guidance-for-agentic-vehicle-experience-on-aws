"""RSA (Roadside Service Assistance) client interface.

No public API documentation exists for either vendor named in spec § D5
(Agero/Urgently are B2B networks with contract-gated integrations). The
interface is designed from first principles; see docs/tech.md §
"RSA stub dispatch — verified API surface" Part A for per-field justification.

Selection: set env var VSA_RSA_CLIENT=fake (default) or =real.

A deploy that silently attempted a real dispatch is this spec's worst failure
mode; therefore the default is "fake" and a test pins it.

Lifecycle status constants (F2.3):
All six states and the terminal subset are defined here once and imported by
Groups 2 and 3. Three hardcoded copies would drift invisibly; spec R7's
ConditionExpression depends on exactly this terminal set.

Timestamp helper (F10.13 / G3-D3):
``iso_now()`` is the single-copy G3-D3 helper imported by both lifecycle.py
and handler.py (consumer). A triple implementation — lifecycle, consumer, and
test helpers — would drift invisibly; the single copy here is the anchor.
"""
from __future__ import annotations

import datetime as _datetime
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Lifecycle status constants (F2.3)
# Define once here; import in Groups 2 and 3. Never write bare string literals.
# ---------------------------------------------------------------------------

LIFECYCLE_REQUESTED: str = "REQUESTED"
LIFECYCLE_DISPATCHED: str = "DISPATCHED"
LIFECYCLE_EN_ROUTE: str = "EN_ROUTE"
LIFECYCLE_ARRIVED: str = "ARRIVED"
LIFECYCLE_CANCELLED: str = "CANCELLED"
LIFECYCLE_FAILED: str = "FAILED"

# Terminal states: once a dispatch reaches one of these, it must never be
# advanced further. Spec R7's ConditionExpression enforces this server-side;
# Group 2's consumer and Group 3.2's lifecycle advancer both key on this set.
LIFECYCLE_TERMINAL_STATES: frozenset[str] = frozenset(
    {LIFECYCLE_ARRIVED, LIFECYCLE_CANCELLED, LIFECYCLE_FAILED}
)


# ---------------------------------------------------------------------------
# Timestamp helper (G3-D3 / F10.13 single-copy rule)
#
# ISO-8601 UTC with milliseconds and Z suffix: ``2026-01-02T03:04:23.999Z``
# Reads the clock exactly once so seconds and milliseconds come from the same
# instant (a double-call could cross a second boundary).
# ---------------------------------------------------------------------------

def iso_now() -> str:
    """Return the current UTC time as ISO-8601 with milliseconds and a Z suffix.

    G3-D3 canonical format: ``YYYY-MM-DDTHH:MM:SS.mmmZ``

    This is the single authoritative implementation for lifecycle.py,
    handler.py (consumer), and test helpers. Import it with:
        from shared.rsa_client import iso_now
    """
    ts = _datetime.datetime.now(_datetime.timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class DispatchAck:
    """Acknowledgement returned by the roadside provider on a successful dispatch.

    Shape is INFERRED — no public vendor doc exists. See docs/tech.md §
    "RSA stub dispatch" Part A for per-field justification.
    """

    provider_ref: str  # Vendor's own id; required for get_status()/cancel() round-trip.
                       # FakeRsaProvider prefixes "SIM-" per spec R4.
                       # IMPORTANT: This is an opaque correlation identifier — NOT an
                       # authentication or authorization token. It is a 48-bit truncation
                       # of a SHA-256 digest (in the fake), sized for demo uniqueness, not
                       # for security. Do not build capability-token access control on it.
    eta_minutes: int   # Relative minutes — NOT an absolute timestamp. An absolute time
                       # from a provider with clock skew becomes a wrong countdown in
                       # Group 6; a relative int replays identically for determinism.
    simulated: bool    # spec R4, non-negotiable. Never inferred from the SIM- prefix.
    status: str        # Maps onto the spec § Design lifecycle enum.


@dataclass(frozen=True)
class DispatchStatus:
    """Current status of an in-flight or completed dispatch."""

    provider_ref: str
    status: str
    eta_minutes: int | None
    simulated: bool


@runtime_checkable
class RsaProvider(Protocol):
    """Protocol every RSA provider implementation must satisfy."""

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
        """Dispatch a roadside service request.

        dispatch_id is the server-generated table PK (e.g. a UUID assigned by
        Group 2 before calling this method). It must NEVER be caller-supplied
        input passed straight through — the fake derives provider_ref from it,
        and a client-controlled dispatch_id would let a caller influence the
        opaque vendor reference. See F2.3 / R2.

        PII WARNING: vin, lat, lon, and callback_contact are personally identifiable
        information. They MUST NOT be logged. The primary risk lands in Group 2's
        consumer (lambdas/rsa-dispatch-consumer/handler.py) — pair this docstring
        with a grep-guard test over that consumer's source once it exists.
        """
        ...

    def get_status(self, provider_ref: str) -> DispatchStatus: ...

    def cancel(self, provider_ref: str) -> None: ...


def get_client() -> RsaProvider:
    """Return the configured RSA provider.

    Reads VSA_RSA_CLIENT env var (default: "fake").
    Raises ValueError on an unknown mode.
    """
    mode = os.environ.get("VSA_RSA_CLIENT", "fake")
    if mode == "fake":
        from . import rsa_client_fake  # local import keeps boto3 out of hot path

        return rsa_client_fake.FakeRsaProvider()
    if mode == "real":
        from . import rsa_client_real

        return rsa_client_real.RealRsaProvider()
    raise ValueError(f"Unknown VSA_RSA_CLIENT mode: {mode!r}")


def is_covered(tenant_id: str, vin: str) -> bool:
    """Return True when the tenant/VIN combination is entitled to roadside dispatch.

    THIS IS A STUB. No coverage model exists anywhere in this portfolio.
    Neither the Connected Services (CS) module nor the DMS module carries any
    concept of RSA entitlement — DMS's warranty_claims.py is claims processing,
    not coverage. Phase A § D7's facet table explicitly designates coverage as
    CS's responsibility, and CS does not implement it.

    See: issues/2026-09-23-rsa-coverage-model-does-not-exist/report.md

    SAFETY CONSTRAINT: is_covered() is an entitlement decision governing dispatch
    of physical equipment at someone's expense — spec § Tier classification calls
    this safety, money, AND legal. In fake mode it returns True (correct accelerator
    posture for demo). In any other mode it raises NotImplementedError rather than
    returning False silently: a silent False reads as a legitimate "not covered"
    verdict and would send a stranded driver away, which is the wrong failure
    direction for a safety path. The only defense today is RealRsaProvider raising
    at request_dispatch; that defense disappears when a downstream implementer fills
    in the vendor stub without implementing a real coverage model.

    Args:
        tenant_id: The tenant (OEM/fleet operator) identifier.
        vin: Vehicle identification number.

    Returns:
        True unconditionally (in fake mode) until a real coverage model is implemented.

    Raises:
        NotImplementedError: When VSA_RSA_CLIENT is anything other than "fake".
            Raise, do not return False — see safety constraint above.
    """
    mode = os.environ.get("VSA_RSA_CLIENT", "fake")
    if mode != "fake":
        raise NotImplementedError(
            f"is_covered() is a stub with no real coverage model. "
            f"In mode {mode!r} it must not silently return True or False — "
            "see issues/2026-09-23-rsa-coverage-model-does-not-exist/ and "
            "spec § Tier classification (safety, money, legal). "
            "Implement a real coverage model before deploying with a real provider."
        )
    return True
