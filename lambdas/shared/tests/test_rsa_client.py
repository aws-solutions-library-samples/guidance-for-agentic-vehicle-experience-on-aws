"""Tests for the RSA client seam (Tasks 1.2 and 1.3).

Three mutation guards are mandated by Task 1.2:
  (a) get_client() default must be "fake" — a test pins it
  (b) FakeRsaProvider.request_dispatch must prefix provider_ref with "SIM-"
  (c) FakeRsaProvider.request_dispatch must set simulated=True

One coverage-stub test is mandated by Task 1.3:
  is_covered() returns True and its docstring states the gap

Fix Group 1 adds (F1.1–F1.5, F1.7):
  (F1.1) Golden ETA literal pinned: _deterministic_eta("1FMCU0F70MUA12345", "tow") == 19
  (F1.2) ack.status == "DISPATCHED" (not just isinstance str)
  (F1.3) inspect.signature() parity across Protocol / fake / real
  (F1.4) dispatch_id uniqueness: two calls differing only in dispatch_id yield different provider_ref
  (F1.5) get_status() round-trip: status.provider_ref == ref passed in

Fix Group 2 adds (F2.1–F2.4):
  (F2.1) Public-path golden ETA: request_dispatch(...).eta_minutes == 19 (not just private helper)
  (F2.2) Non-invertibility: provider_ref must NOT contain dispatch_id (opaque ref)
  (F2.3) Lifecycle constants: LIFECYCLE_TERMINAL_STATES pins the exact terminal set
  (F2.4) Dead import os removed (was unused after F1.7 removed sys.modules evictions)

Run with:
  .venv/bin/python3 -m pytest lambdas/shared/tests/test_rsa_client.py -v
"""
from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# Task 1.2 — Protocol, selector, fake, real
# ---------------------------------------------------------------------------


class TestGetClientDefault:
    """Mutation guard (a): the default mode must be 'fake'.

    If this test fails, that proves the default is NOT "fake" —
    i.e., it is the expected failure when the mutation (default → "real") is applied.
    """

    def test_default_mode_is_fake(self, monkeypatch):
        """get_client() with no env var set returns a FakeRsaProvider."""
        monkeypatch.delenv("VSA_RSA_CLIENT", raising=False)

        from lambdas.shared import rsa_client, rsa_client_fake

        client = rsa_client.get_client()
        assert isinstance(client, rsa_client_fake.FakeRsaProvider), (
            f"Expected FakeRsaProvider but got {type(client).__name__}. "
            "The default mode must be 'fake'; a silent real dispatch is the worst failure."
        )

    def test_explicit_fake_returns_fake_provider(self, monkeypatch):
        monkeypatch.setenv("VSA_RSA_CLIENT", "fake")

        from lambdas.shared import rsa_client, rsa_client_fake

        client = rsa_client.get_client()
        assert isinstance(client, rsa_client_fake.FakeRsaProvider)

    def test_explicit_real_returns_real_provider(self, monkeypatch):
        monkeypatch.setenv("VSA_RSA_CLIENT", "real")

        from lambdas.shared import rsa_client, rsa_client_real

        client = rsa_client.get_client()
        assert isinstance(client, rsa_client_real.RealRsaProvider)

    def test_unknown_mode_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("VSA_RSA_CLIENT", "vendor-x")

        from lambdas.shared import rsa_client

        with pytest.raises(ValueError, match="vendor-x"):
            rsa_client.get_client()


class TestFakeRsaProviderSimPrefix:
    """Mutation guard (b): provider_ref must be prefixed 'SIM-'.

    If this test fails, that proves the prefix is absent —
    i.e., the expected failure when the 'SIM-' prefix mutation is applied.
    """

    def test_provider_ref_is_prefixed_with_sim(self):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        assert ack.provider_ref.startswith("SIM-"), (
            f"provider_ref {ack.provider_ref!r} must start with 'SIM-' per spec R4. "
            "Dropping this prefix removes the ability to identify simulated dispatches "
            "in downstream code without relying on the simulated flag."
        )


class TestFakeRsaProviderSimulatedFlag:
    """Mutation guard (c): simulated must be True.

    If this test fails, that proves simulated is not True —
    i.e., the expected failure when the simulated=True mutation is applied.
    """

    def test_simulated_is_true(self):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        assert ack.simulated is True, (
            "ack.simulated must be True per spec R4. This flag must never be inferred "
            "from the SIM- prefix — it must be set explicitly on the ack."
        )


# ---------------------------------------------------------------------------
# F1.1 — Pin the golden ETA literal (Fix Group 1 Critical)
# ---------------------------------------------------------------------------


class TestFakeRsaProviderDeterminism:
    """Task 1.2 Accept 4: ETA must be deterministic given the same inputs.

    F1.1 adds the golden-value pin: the exact output for the canonical inputs is
    asserted as a literal so a cross-process violation (e.g., builtin hash() which
    is PYTHONHASHSEED-salted) fails here even though intra-process comparisons pass.
    """

    def test_eta_golden_value_is_19(self):
        """Pin the exact ETA for the canonical test inputs.

        Mutation guard: replace hashlib.sha256 with builtin hash() (PYTHONHASHSEED-salted)
        or lru_cache over random.randint — both pass intra-process comparisons but return
        a different ETA on every cold start. This test fails against both violations because
        it asserts the cross-process constant, not just intra-process stability.

        The constant 19 is architect-verified across three cold starts with:
            vin="1FMCU0F70MUA12345", service_type="tow"
        If this value ever legitimately changes, update the literal and record why in
        decisions.md — the whole point is that you cannot change the implementation
        without breaking this test.
        """
        from lambdas.shared.rsa_client_fake import _deterministic_eta

        assert _deterministic_eta("1FMCU0F70MUA12345", "tow") == 19, (
            "Golden ETA for vin='1FMCU0F70MUA12345', service_type='tow' must be 19. "
            "If the hash function was changed from hashlib.sha256 to a process-salted "
            "alternative (e.g., builtin hash()), this test correctly fails. "
            "A demo that shows a different ETA on every replay is not demoable."
        )

    def test_eta_is_deterministic_same_inputs(self):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        kwargs = dict(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        ack1 = provider.request_dispatch(**kwargs)
        ack2 = provider.request_dispatch(**kwargs)
        assert ack1.eta_minutes == ack2.eta_minutes, (
            "ETA must be deterministic: same inputs must always produce the same ETA."
        )

    def test_eta_differs_for_different_vins(self):
        """Different VINs give different ETAs (fixed inputs, deterministic output)."""
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        etas = {
            provider.request_dispatch(
                vin=f"VIN{i:017d}",
                lat=37.7749,
                lon=-122.4194,
                service_type="tow",
                tenant_id="meridian",
                callback_contact="+15551234567",
                dispatch_id=f"DISP-{i:08X}",
            ).eta_minutes
            for i in range(10)
        }
        # With 28 possible values and 10 VINs, at least 2 distinct ETAs expected.
        assert len(etas) > 1, "Deterministic ETA should vary across different VINs."

    def test_eta_is_in_reasonable_range(self):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        assert 18 <= ack.eta_minutes <= 45, (
            f"ETA {ack.eta_minutes} is outside the expected 18–45 minute range."
        )


# ---------------------------------------------------------------------------
# F1.2 — Assert ack status value, not just type
# ---------------------------------------------------------------------------


class TestFakeRsaProviderAckShape:
    """DispatchAck has the expected fields and types.

    F1.2 adds the status-value assertion: ack.status must be "DISPATCHED", not just
    a non-empty string. Task 2.2 writes the DynamoDB row from this ack; a status of
    "REQUESTED" written back recreates the dead letterbox this spec exists to close.
    """

    def test_ack_status_is_dispatched(self):
        """F1.2 mutation guard: fake returns "REQUESTED" → MUST fail."""
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        assert ack.status == "DISPATCHED", (
            f"ack.status must be 'DISPATCHED', got {ack.status!r}. "
            "Task 2.2 writes the DynamoDB row from this ack; a 'REQUESTED' status "
            "written back recreates the dead letterbox this spec exists to close."
        )

    def test_ack_has_required_fields(self):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        assert isinstance(ack.provider_ref, str) and ack.provider_ref
        assert isinstance(ack.eta_minutes, int)
        assert isinstance(ack.simulated, bool)
        assert isinstance(ack.status, str) and ack.status

    def test_ack_is_frozen(self):
        """DispatchAck is a frozen dataclass — must not be mutatable."""
        from dataclasses import FrozenInstanceError

        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        with pytest.raises(FrozenInstanceError):
            ack.simulated = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# F1.3 — Signature parity across Protocol / fake / real
# ---------------------------------------------------------------------------


class TestSignatureParity:
    """F1.3: inspect.signature() must match across Protocol, fake, and real.

    runtime_checkable isinstance checks method names only — demonstrated by the
    reviewer using a class with three zero-arg methods satisfying the Protocol.
    Spec R2's "swapping in a real one is a client change, not a redesign" rests on
    this parity; signature drift would surface as a TypeError at call time instead of
    the informative NotImplementedError the seam exists to raise.

    Mutation guard: drop or rename one parameter on the fake → MUST fail.
    """

    def _get_signatures(self, cls, method: str) -> inspect.Signature:
        return inspect.signature(getattr(cls, method))

    def test_request_dispatch_signatures_match(self):
        from lambdas.shared.rsa_client import RsaProvider
        from lambdas.shared.rsa_client_fake import FakeRsaProvider
        from lambdas.shared.rsa_client_real import RealRsaProvider

        proto_sig = self._get_signatures(RsaProvider, "request_dispatch")
        fake_sig = self._get_signatures(FakeRsaProvider, "request_dispatch")
        real_sig = self._get_signatures(RealRsaProvider, "request_dispatch")

        proto_params = list(proto_sig.parameters)
        fake_params = list(fake_sig.parameters)
        real_params = list(real_sig.parameters)

        assert proto_params == fake_params, (
            f"request_dispatch signature mismatch: Protocol {proto_params} != "
            f"FakeRsaProvider {fake_params}. Spec R2 requires drop-in replaceability."
        )
        assert proto_params == real_params, (
            f"request_dispatch signature mismatch: Protocol {proto_params} != "
            f"RealRsaProvider {real_params}. Spec R2 requires drop-in replaceability."
        )

    def test_get_status_signatures_match(self):
        from lambdas.shared.rsa_client import RsaProvider
        from lambdas.shared.rsa_client_fake import FakeRsaProvider
        from lambdas.shared.rsa_client_real import RealRsaProvider

        proto_params = list(self._get_signatures(RsaProvider, "get_status").parameters)
        fake_params = list(self._get_signatures(FakeRsaProvider, "get_status").parameters)
        real_params = list(self._get_signatures(RealRsaProvider, "get_status").parameters)

        assert proto_params == fake_params, (
            f"get_status signature mismatch: Protocol {proto_params} != "
            f"FakeRsaProvider {fake_params}."
        )
        assert proto_params == real_params, (
            f"get_status signature mismatch: Protocol {proto_params} != "
            f"RealRsaProvider {real_params}."
        )

    def test_cancel_signatures_match(self):
        from lambdas.shared.rsa_client import RsaProvider
        from lambdas.shared.rsa_client_fake import FakeRsaProvider
        from lambdas.shared.rsa_client_real import RealRsaProvider

        proto_params = list(self._get_signatures(RsaProvider, "cancel").parameters)
        fake_params = list(self._get_signatures(FakeRsaProvider, "cancel").parameters)
        real_params = list(self._get_signatures(RealRsaProvider, "cancel").parameters)

        assert proto_params == fake_params, (
            f"cancel signature mismatch: Protocol {proto_params} != "
            f"FakeRsaProvider {fake_params}."
        )
        assert proto_params == real_params, (
            f"cancel signature mismatch: Protocol {proto_params} != "
            f"RealRsaProvider {real_params}."
        )


# ---------------------------------------------------------------------------
# F1.4 — provider_ref uniqueness per dispatch_id
# ---------------------------------------------------------------------------


class TestProviderRefUniqueness:
    """F1.4: provider_ref must differ when dispatch_id differs.

    The old formula SIM-{vin[:8]}-{service_type[:4]} produced the same ref for two
    distinct dispatches of the same vehicle and service type. Task 2.2 Accept 4 requires
    idempotency keyed on the stream record / dispatchId; if idempotency had used
    provider_ref instead, a legitimate second tow would be silently dropped.

    Mutation guard: two calls differing only in dispatch_id → a uniqueness test MUST fail
    if the ref ignores dispatch_id.
    """

    def test_distinct_dispatch_ids_yield_distinct_refs(self):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        base_kwargs = dict(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
        )
        ack1 = provider.request_dispatch(**base_kwargs, dispatch_id="DISP-00000001")
        ack2 = provider.request_dispatch(**base_kwargs, dispatch_id="DISP-00000002")

        assert ack1.provider_ref != ack2.provider_ref, (
            f"Two dispatches for the same vehicle/service but different dispatch_ids must "
            f"yield different provider_refs. Got {ack1.provider_ref!r} for both. "
            "A collision makes Group 2's idempotency indistinguishable from a legitimate "
            "second dispatch."
        )

    def test_same_dispatch_id_yields_same_ref(self):
        """Determinism: same dispatch_id always yields the same provider_ref."""
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        kwargs = dict(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-00000001",
        )
        ack1 = provider.request_dispatch(**kwargs)
        ack2 = provider.request_dispatch(**kwargs)
        assert ack1.provider_ref == ack2.provider_ref, (
            "Same dispatch_id must always yield the same provider_ref."
        )

    def test_provider_ref_still_prefixed_sim(self):
        """SIM- prefix must survive the F1.4 dispatch_id change."""
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-00000001",
        )
        assert ack.provider_ref.startswith("SIM-"), (
            f"provider_ref {ack.provider_ref!r} must still start with 'SIM-' per spec R4 "
            "after the F1.4 dispatch_id change."
        )


# ---------------------------------------------------------------------------
# F1.5 — get_status() round-trip identity
# ---------------------------------------------------------------------------


class TestFakeRsaProviderProtocolCompliance:
    """FakeRsaProvider satisfies the RsaProvider Protocol.

    F1.5 adds the round-trip identity assertion: status.provider_ref must equal the ref
    passed in. Previously only isinstance(provider_ref, str) was checked — a lost or
    hardcoded ref would pass. docs/tech.md Part A states provider_ref is REQUIRED for the
    get_status()/cancel() round-trip.

    Mutation guard: fake returns a hardcoded ref → MUST fail.
    """

    def test_fake_is_protocol_compatible(self):
        from lambdas.shared.rsa_client import RsaProvider
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        assert isinstance(provider, RsaProvider), (
            "FakeRsaProvider must satisfy the RsaProvider protocol."
        )

    def test_get_status_round_trip_identity(self):
        """F1.5 mutation guard: fake returns hardcoded ref → MUST fail."""
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ref = "SIM-DISP-DEADBEEF"
        status = provider.get_status(ref)
        assert status.provider_ref == ref, (
            f"get_status(ref).provider_ref must equal ref. "
            f"Got {status.provider_ref!r}, expected {ref!r}. "
            "docs/tech.md Part A states provider_ref is required for round-trip "
            "in get_status()/cancel()."
        )

    def test_get_status_returns_dispatch_status(self):
        from lambdas.shared.rsa_client import DispatchStatus
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        status = provider.get_status("SIM-1FMCU0F7-TOW")
        assert isinstance(status, DispatchStatus)
        assert status.simulated is True

    def test_cancel_returns_none(self):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        result = provider.cancel("SIM-1FMCU0F7-TOW")
        assert result is None


# ---------------------------------------------------------------------------
# Task 1.2 Accept 3 — Real provider seam
# ---------------------------------------------------------------------------


class TestRealRsaProviderSeam:
    """Task 1.2 Accept 3: rsa_client_real.py raises NotImplementedError with a helpful message."""

    def test_real_request_dispatch_raises_not_implemented(self):
        from lambdas.shared.rsa_client_real import RealRsaProvider

        provider = RealRsaProvider()
        with pytest.raises(NotImplementedError) as exc_info:
            provider.request_dispatch(
                vin="1FMCU0F70MUA12345",
                lat=37.7749,
                lon=-122.4194,
                service_type="tow",
                tenant_id="meridian",
                callback_contact="+15551234567",
                dispatch_id="DISP-ABCDEF01",
            )
        msg = str(exc_info.value).lower()
        # The message must name distinct requirements for a real integration.
        assert "credentials" in msg, (
            "NotImplementedError message must mention 'credentials' — a real integration "
            "requires partner credentials (Agero or Urgently)."
        )
        assert "per-tenant" in msg or "routing" in msg, (
            "NotImplementedError message must mention per-tenant routing — each OEM fleet "
            "may have a different roadside program and account number."
        )

    def test_real_get_status_raises_not_implemented(self):
        from lambdas.shared.rsa_client_real import RealRsaProvider

        provider = RealRsaProvider()
        with pytest.raises(NotImplementedError):
            provider.get_status("SIM-1FMCU0F7-TOW")

    def test_real_cancel_raises_not_implemented(self):
        from lambdas.shared.rsa_client_real import RealRsaProvider

        provider = RealRsaProvider()
        with pytest.raises(NotImplementedError):
            provider.cancel("SIM-1FMCU0F7-TOW")

    def test_real_is_protocol_compatible_type_check(self):
        """RealRsaProvider satisfies the protocol at the structural level (has all methods)."""
        from lambdas.shared.rsa_client import RsaProvider
        from lambdas.shared.rsa_client_real import RealRsaProvider

        provider = RealRsaProvider()
        assert isinstance(provider, RsaProvider)


# ---------------------------------------------------------------------------
# Task 1.3 — Coverage stub
# ---------------------------------------------------------------------------


class TestIsCoveredStub:
    """Task 1.3 Accept 1: is_covered() returns True unconditionally.

    This is a presence-and-behaviour test: the stub must exist, must return True,
    and its docstring must state the gap explicitly.
    """

    def test_is_covered_returns_true(self):
        from lambdas.shared.rsa_client import is_covered

        assert is_covered("meridian", "1FMCU0F70MUA12345") is True

    def test_is_covered_returns_true_for_any_inputs(self):
        """Unconditional True — any tenant/VIN combination returns True."""
        from lambdas.shared.rsa_client import is_covered

        assert is_covered("unknown-tenant", "ANYVIN0000000000000") is True
        assert is_covered("", "") is True

    def test_is_covered_docstring_states_gap(self):
        """The stub's docstring must explicitly state that no coverage model exists.

        This is the mutation guard for Task 1.3: a stub that silently returns True
        without documentation is the same defect shape as the dead letterbox.
        If this test fails after the mutation (remove the gap statement from the
        docstring), the test correctly identifies the property is gone.
        """
        from lambdas.shared.rsa_client import is_covered

        doc = is_covered.__doc__ or ""
        assert "no coverage model" in doc.lower(), (
            "is_covered docstring must state plainly that no coverage model exists. "
            "Task 1.3 Accept 1 and D7 require the gap be documented, not hidden. "
            "The exact phrase 'no coverage model' must appear in the docstring."
        )

    def test_is_covered_accepts_tenant_and_vin(self):
        """is_covered takes (tenant_id, vin) positional args — correct signature."""
        from lambdas.shared.rsa_client import is_covered

        sig = inspect.signature(is_covered)
        params = list(sig.parameters)
        assert params == ["tenant_id", "vin"], (
            f"is_covered signature must be (tenant_id, vin), got {params}."
        )



# ---------------------------------------------------------------------------
# F2.1 — Public-path golden ETA (Fix Group 2 Critical)
# ---------------------------------------------------------------------------


class TestPublicPathGoldenETA:
    """F2.1: Assert eta_minutes == 19 through request_dispatch, not through the private helper.

    F1.1 pins the literal on _deterministic_eta directly. Cycle 2's Critical shows a
    mutation (lru_cache over random.randint) placed *inside* request_dispatch while leaving
    the private helper intact survives all 31 tests including F1.1's golden pin — because
    F1.1's assertion never calls request_dispatch. This test closes that gap.

    Mutation guard: put lru_cache'd random.randint inside request_dispatch (not the helper)
    → this test MUST fail. The helper-level test (test_eta_golden_value_is_19) still passes
    against that mutation; only this test fails, which is the point.
    """

    # Canonical inputs shared with F1.1 so both tests assert the same constant.
    _VIN = "1FMCU0F70MUA12345"
    _SERVICE_TYPE = "tow"
    _DISPATCH_ID = "DISP-ABCDEF01"
    _EXPECTED_ETA = 19  # Architect-verified across three cold starts.

    def _canonical_kwargs(self) -> dict:
        return dict(
            vin=self._VIN,
            lat=37.7749,
            lon=-122.4194,
            service_type=self._SERVICE_TYPE,
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id=self._DISPATCH_ID,
        )

    def test_request_dispatch_eta_golden_value(self):
        """Pin the exact ETA through the public path.

        If this fails: the implementation of request_dispatch is not producing the
        expected deterministic value. This catches a mutation that bypasses
        _deterministic_eta inside request_dispatch while leaving the helper intact.

        If the golden value ever legitimately changes (e.g., the seed formula changes),
        update the literal here AND in test_eta_golden_value_is_19, and record why in
        decisions.md — the whole point is you cannot silently change behaviour.
        """
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(**self._canonical_kwargs())
        assert ack.eta_minutes == self._EXPECTED_ETA, (
            f"request_dispatch(...).eta_minutes must be {self._EXPECTED_ETA} for the "
            f"canonical inputs (vin={self._VIN!r}, service_type={self._SERVICE_TYPE!r}). "
            "A cross-process violation (e.g., lru_cache'd random.randint inside "
            "request_dispatch) would yield a different value here on each cold start "
            "while the helper-level test still passes — this test catches it."
        )

    def test_request_dispatch_eta_matches_helper(self):
        """Both the public path and the private helper return the same value.

        Belt-and-suspenders: if the helper's output and request_dispatch's output
        ever diverge, something has gone wrong in the call chain.
        """
        from lambdas.shared.rsa_client_fake import FakeRsaProvider, _deterministic_eta

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(**self._canonical_kwargs())
        helper_eta = _deterministic_eta(self._VIN, self._SERVICE_TYPE)
        assert ack.eta_minutes == helper_eta, (
            f"request_dispatch(...).eta_minutes ({ack.eta_minutes}) must equal "
            f"_deterministic_eta(...) ({helper_eta}). They should never diverge."
        )


# ---------------------------------------------------------------------------
# F2.2 — provider_ref opacity (Fix Group 2 Warning)
# ---------------------------------------------------------------------------


class TestProviderRefOpacity:
    """F2.2: provider_ref must NOT be invertible back to dispatch_id.

    After F1.4, provider_ref was f"SIM-{dispatch_id}", so removeprefix("SIM-") exactly
    recovered dispatch_id. A real vendor's reference is opaque; leaving it invertible
    invites Group 2/3 consumers to recover dispatchId from providerRef — which passes
    every stubbed test and breaks on the first real provider (falsifying spec R2).

    Mutation guard: revert to f"SIM-{dispatch_id}" → non-invertibility test MUST fail.
    """

    _DISPATCH_ID = "DISP-ABCDEF01"

    def _dispatch(self, dispatch_id: str):
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        return provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id=dispatch_id,
        )

    def test_provider_ref_does_not_contain_dispatch_id(self):
        """F2.2 mutation guard: revert to f"SIM-{dispatch_id}" → this test MUST fail.

        If dispatch_id appears literally inside provider_ref, the ref is invertible and
        any consumer can recover the table PK from the vendor reference — which breaks
        on the first real provider.
        """
        ack = self._dispatch(self._DISPATCH_ID)
        assert self._DISPATCH_ID not in ack.provider_ref, (
            f"provider_ref {ack.provider_ref!r} must NOT contain dispatch_id "
            f"{self._DISPATCH_ID!r}. An invertible ref invites consumers to reverse "
            "dispatchId from providerRef, which passes all stubbed tests but breaks "
            "on any real provider that returns an opaque reference. Spec R2 requires "
            "the fake to be a realistic stand-in."
        )

    def test_provider_ref_still_prefixed_sim_after_f2_2(self):
        """SIM- prefix must still hold after switching to the hashed formula."""
        ack = self._dispatch(self._DISPATCH_ID)
        assert ack.provider_ref.startswith("SIM-"), (
            f"provider_ref {ack.provider_ref!r} must start with 'SIM-' per spec R4."
        )

    def test_provider_ref_distinct_for_different_dispatch_ids_after_f2_2(self):
        """Uniqueness must still hold under the hashed formula (F1.4 re-verified)."""
        ack1 = self._dispatch("DISP-00000001")
        ack2 = self._dispatch("DISP-00000002")
        assert ack1.provider_ref != ack2.provider_ref, (
            "Distinct dispatch_ids must still produce distinct provider_refs after F2.2."
        )

    def test_provider_ref_deterministic_for_same_dispatch_id_after_f2_2(self):
        """Same dispatch_id must yield the same provider_ref (F1.4 re-verified)."""
        ack1 = self._dispatch(self._DISPATCH_ID)
        ack2 = self._dispatch(self._DISPATCH_ID)
        assert ack1.provider_ref == ack2.provider_ref, (
            "Same dispatch_id must yield the same provider_ref after F2.2."
        )

    def test_public_path_eta_golden_value_after_f2_2(self):
        """F2.1 golden value must hold after F2.2 changes provider_ref formula.

        F2.2 only changes provider_ref, not ETA, so 19 must remain.
        """
        ack = self._dispatch("DISP-ABCDEF01")
        assert ack.eta_minutes == 19, (
            f"ETA must still be 19 after F2.2. Got {ack.eta_minutes}."
        )


# ---------------------------------------------------------------------------
# F2.3 — Lifecycle status constants (Fix Group 2 top Suggestion)
# ---------------------------------------------------------------------------


class TestLifecycleConstants:
    """F2.3: Lifecycle status constants are defined in rsa_client.py and importable.

    Spec R7's ConditionExpression depends on exactly the terminal set. Groups 2 and 3
    are both writers; without a shared constant, three hardcoded copies would drift
    invisibly. The terminal set is ARRIVED, CANCELLED, FAILED — no more, no less.

    Mutation guard: remove ARRIVED from LIFECYCLE_TERMINAL_STATES → this test MUST fail.
    """

    def test_all_lifecycle_constants_defined(self):
        """All six lifecycle states are importable from rsa_client."""
        from lambdas.shared.rsa_client import (
            LIFECYCLE_ARRIVED,
            LIFECYCLE_CANCELLED,
            LIFECYCLE_DISPATCHED,
            LIFECYCLE_EN_ROUTE,
            LIFECYCLE_FAILED,
            LIFECYCLE_REQUESTED,
        )
        assert LIFECYCLE_REQUESTED == "REQUESTED"
        assert LIFECYCLE_DISPATCHED == "DISPATCHED"
        assert LIFECYCLE_EN_ROUTE == "EN_ROUTE"
        assert LIFECYCLE_ARRIVED == "ARRIVED"
        assert LIFECYCLE_CANCELLED == "CANCELLED"
        assert LIFECYCLE_FAILED == "FAILED"

    def test_terminal_states_set_is_exact(self):
        """F2.3 mutation guard: remove ARRIVED from terminal set → this test MUST fail.

        The set must contain exactly ARRIVED, CANCELLED, FAILED — spec R7 and Task 2.2
        Accept 3 both depend on this. A ConditionExpression built from a set missing
        ARRIVED would allow advancing an arrived dispatch to DISPATCHED or EN_ROUTE.
        """
        from lambdas.shared.rsa_client import (
            LIFECYCLE_ARRIVED,
            LIFECYCLE_CANCELLED,
            LIFECYCLE_FAILED,
            LIFECYCLE_TERMINAL_STATES,
        )
        assert LIFECYCLE_TERMINAL_STATES == frozenset(
            {LIFECYCLE_ARRIVED, LIFECYCLE_CANCELLED, LIFECYCLE_FAILED}
        ), (
            f"LIFECYCLE_TERMINAL_STATES must be exactly "
            f"{{ARRIVED, CANCELLED, FAILED}}. Got {LIFECYCLE_TERMINAL_STATES!r}. "
            "Spec R7's ConditionExpression prevents advancing out of terminal states; "
            "an incomplete set allows illegal transitions."
        )

    def test_terminal_states_does_not_include_non_terminal(self):
        """REQUESTED, DISPATCHED, EN_ROUTE must NOT be terminal (they are transient)."""
        from lambdas.shared.rsa_client import (
            LIFECYCLE_DISPATCHED,
            LIFECYCLE_EN_ROUTE,
            LIFECYCLE_REQUESTED,
            LIFECYCLE_TERMINAL_STATES,
        )
        for state in (LIFECYCLE_REQUESTED, LIFECYCLE_DISPATCHED, LIFECYCLE_EN_ROUTE):
            assert state not in LIFECYCLE_TERMINAL_STATES, (
                f"State {state!r} must NOT be in LIFECYCLE_TERMINAL_STATES — "
                "it is a transient state, not a terminal one."
            )

    def test_terminal_states_is_frozenset(self):
        """LIFECYCLE_TERMINAL_STATES must be immutable (frozenset, not set)."""
        from lambdas.shared.rsa_client import LIFECYCLE_TERMINAL_STATES

        assert isinstance(LIFECYCLE_TERMINAL_STATES, frozenset), (
            f"LIFECYCLE_TERMINAL_STATES must be a frozenset to prevent accidental mutation. "
            f"Got {type(LIFECYCLE_TERMINAL_STATES).__name__!r}."
        )

    def test_fake_uses_lifecycle_constant_for_status(self):
        """FakeRsaProvider uses LIFECYCLE_DISPATCHED constant, not a bare string."""
        from lambdas.shared.rsa_client import LIFECYCLE_DISPATCHED
        from lambdas.shared.rsa_client_fake import FakeRsaProvider

        provider = FakeRsaProvider()
        ack = provider.request_dispatch(
            vin="1FMCU0F70MUA12345",
            lat=37.7749,
            lon=-122.4194,
            service_type="tow",
            tenant_id="meridian",
            callback_contact="+15551234567",
            dispatch_id="DISP-ABCDEF01",
        )
        assert ack.status == LIFECYCLE_DISPATCHED, (
            f"ack.status must equal LIFECYCLE_DISPATCHED ({LIFECYCLE_DISPATCHED!r}). "
            f"Got {ack.status!r}. The fake must use the shared constant, not a literal."
        )



# ---------------------------------------------------------------------------
# F3.1 — is_covered() raises when mode != "fake" (Fix Group 3 Warning)
# ---------------------------------------------------------------------------


class TestIsCoveredFailSafe:
    """F3.1: is_covered() must raise when VSA_RSA_CLIENT is not "fake".

    is_covered() is an entitlement decision governing dispatch of physical equipment
    at someone's expense — spec § Tier classification calls this safety, money, AND
    legal. Returning True unconditionally (including in real mode) was the Warning.

    The constraint is RAISE, not return False. A silent False reads as a legitimate
    "not covered" verdict and would send a stranded driver away — the wrong failure
    for a safety path. In fake mode it must still return True.

    Mutation guard: make is_covered() return True unconditionally → the real-mode
    test MUST fail.
    """

    def test_is_covered_returns_true_in_fake_mode(self, monkeypatch):
        """Fake mode: is_covered still returns True (correct accelerator posture)."""
        monkeypatch.setenv("VSA_RSA_CLIENT", "fake")

        from lambdas.shared.rsa_client import is_covered

        assert is_covered("meridian", "1FMCU0F70MUA12345") is True

    def test_is_covered_returns_true_with_no_env(self, monkeypatch):
        """Default (no env var, default = fake): is_covered returns True."""
        monkeypatch.delenv("VSA_RSA_CLIENT", raising=False)

        from lambdas.shared.rsa_client import is_covered

        assert is_covered("meridian", "1FMCU0F70MUA12345") is True

    def test_is_covered_raises_in_real_mode(self, monkeypatch):
        """F3.1 mutation guard: unconditional True must fail this test.

        is_covered() in real mode must raise NotImplementedError — not return False
        silently. A silent False would route the driver away as "not covered" without
        any real coverage check, the wrong failure direction for a safety path.
        """
        monkeypatch.setenv("VSA_RSA_CLIENT", "real")

        from lambdas.shared.rsa_client import is_covered

        with pytest.raises(NotImplementedError) as exc_info:
            is_covered("meridian", "1FMCU0F70MUA12345")
        msg = str(exc_info.value)
        assert "stub" in msg.lower() or "coverage model" in msg.lower(), (
            "NotImplementedError message must mention the stub/coverage model gap — "
            "the error must be actionable."
        )


# ---------------------------------------------------------------------------
# F3.4 — Parametrize get_client() fail-closed negative cases
# ---------------------------------------------------------------------------


class TestGetClientFailClosedParametrized:
    """F3.4: get_client() raises ValueError for all near-miss and unexpected modes.

    The property holds today but only one sample is guarded. Case-sensitivity and
    whitespace are exactly the kind of near-miss that later gets "fixed" by loosening
    the comparison (e.g., strip + lower). Parametrize to pin the exact set of inputs
    that must reject.

    Mutation guard: lowercase/strip the mode before comparing → at least one case MUST fail.
    """

    @pytest.mark.parametrize("bad_mode", [
        "",          # empty string
        " ",         # whitespace only
        "Fake",      # wrong case
        "REAL",      # wrong case
        "fake ",     # trailing space
        " fake",     # leading space
        "vendor-x",  # unexpected token
    ])
    def test_bad_mode_raises_value_error(self, monkeypatch, bad_mode):
        """F3.4 mutation guard: lowercase/strip before comparing → at least one case fails.

        If the implementation normalizes the mode (e.g., mode.strip().lower()), then
        "Fake", "REAL", "fake ", etc. would NOT raise and the parametrized case would
        fail here — which is the expected failure when the mutation is applied.
        """
        monkeypatch.setenv("VSA_RSA_CLIENT", bad_mode)

        from lambdas.shared import rsa_client

        with pytest.raises(ValueError, match=repr(bad_mode).replace("(", "\\(").replace(")", "\\)")):
            rsa_client.get_client()



# ---------------------------------------------------------------------------
# F10.13 — iso_now() shared timestamp helper (G3-D3 single-copy)
# ---------------------------------------------------------------------------


class TestIsoNow:
    """F10.13 (G3-D3): shared.rsa_client.iso_now() must produce ISO-8601 UTC with Z suffix.

    This is the single-copy timestamp helper imported by lifecycle.py and handler.py.
    A test here pins the format so a format change anywhere fails at the source.

    Mutation guard: change the format string (e.g., drop milliseconds, drop Z) →
    strptime parse fails or endswith('Z') fails → FAIL.
    """

    def test_iso_now_format(self):
        """iso_now() returns ISO-8601 UTC with milliseconds and Z suffix."""
        import datetime as _dt
        from lambdas.shared.rsa_client import iso_now

        ts = iso_now()
        assert isinstance(ts, str), "iso_now() must return a string."
        assert ts.endswith("Z"), f"iso_now() must end with 'Z', got {ts!r}."
        # Must parse without error
        _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")

    def test_iso_now_is_close_to_utc_now(self):
        """iso_now() must be within 2 seconds of the current UTC time."""
        import datetime as _dt
        from lambdas.shared.rsa_client import iso_now

        ts = iso_now()
        dt = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=_dt.timezone.utc
        )
        now = _dt.datetime.now(_dt.timezone.utc)
        delta = abs((now - dt).total_seconds())
        assert delta < 2, f"iso_now() returned a stale timestamp: delta={delta}s, ts={ts!r}."

    def test_iso_now_reads_clock_once(self):
        """iso_now() reads the clock once — seconds and milliseconds from the same instant.

        Mutation guard: revert to double datetime.now() call → patch so the two calls
        return :23.999999 and :24.000001 → result becomes '...24.000Z' instead of
        '...23.999Z' → FAIL.
        """
        import datetime as _dt
        from unittest.mock import patch as _patch
        from lambdas.shared import rsa_client as _rc

        t1 = _dt.datetime(2026, 1, 2, 3, 4, 23, 999_999, tzinfo=_dt.timezone.utc)
        t2 = _dt.datetime(2026, 1, 2, 3, 4, 24, 1, tzinfo=_dt.timezone.utc)

        call_count = {"n": 0}

        class _FakeDatetime:
            @staticmethod
            def now(tz=None):
                call_count["n"] += 1
                return t1 if call_count["n"] == 1 else t2

        with _patch.object(_rc._datetime, "datetime", _FakeDatetime):
            result = _rc.iso_now()

        assert result == "2026-01-02T03:04:23.999Z", (
            f"iso_now() must read the clock once. Expected '2026-01-02T03:04:23.999Z', "
            f"got {result!r}. Mutation: double-call → '2026-01-02T03:04:24.000Z' → FAIL."
        )
        assert call_count["n"] == 1, (
            f"datetime.now() called {call_count['n']} times; must be exactly 1."
        )
