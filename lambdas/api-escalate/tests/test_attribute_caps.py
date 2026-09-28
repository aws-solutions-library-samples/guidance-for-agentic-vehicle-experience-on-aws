"""Every Connect contact attribute this handler sends is length-bounded.

Security review Cycle 2, W3. Amazon Connect caps the whole attribute MAP at
32KB, so a single unbounded value is a per-request denial: `StartChatContact`
refuses, and on the P0 path that means the driver gets no human. `reason`,
`summary` and `escalationType` were capped; `vin`, `vehicleId`, `tenantId`,
`triageSessionId`, `driverId`, `driverName` and `voiceSessionId` were not.

The caps are set well above any legitimate value (a VIN is 17 characters, ids are
UUID-ish), so nothing real truncates. They bound the worst case.

This is a data-shape contract at a trust boundary, so it is tested by property
rather than by example: the table drives both the per-field cap and the
"no field is unbounded" check, so a field added to `_build_attributes` without a
cap fails `test_every_attribute_is_bounded` even if nobody updates this file.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path and load wiring — copied from test_handler.py deliberately, not
# shortcut to `import handler`. F4.10 / F5.6 moved this suite onto
# spec_from_file_location under the unique name 'escalate_handler' so that
# lambdas/api-escalate/ and lambdas/rsa-dispatch-consumer/ can run in one
# pytest process without colliding on sys.modules["handler"]. A bare
# `import handler` here would reintroduce that collision.
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent

for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("TENANT_CONFIG_TABLE", "test-tenant-config")
os.environ.setdefault("CMS_STAGE", "test")
os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")


def _load_handler():
    mod_name = "escalate_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _LAMBDA_ROOT / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


handler = _load_handler()


# field name in the emitted map -> (kwarg name, cap)
_CAPPED: dict[str, tuple[str, int]] = {
    "vin": ("vin", 32),
    "vehicleId": ("vehicle_id", 64),
    "reason": ("reason", 500),
    "tenantId": ("tenant_id", 64),
    "escalationType": ("escalation_type", 16),
    "summary": ("summary", 2000),
    "triageSessionId": ("triage_session_id", 128),
    "driverId": ("driver_id", 64),
    "driverName": ("display_name", 128),
    "voiceSessionId": ("voice_session_id", 128),
}

# Emitted but not caller-supplied, so not length-bounded by a cap.
_CONSTANT_FIELDS = {"vsaSource", "severity"}


def _attrs(**overrides):
    kwargs = dict(
        severity="P0",
        vin="1HGBH41JXMN000046",
        vehicle_id="VEH-0025",
        reason="brake fault",
        summary="summary text",
        triage_session_id="triage-1",
        driver_id="DRV-1",
        display_name="Sam Carter",
        voice_session_id="voice-1",
        tenant_id="meridian",
        escalation_type="safety",
    )
    kwargs.update(overrides)
    return handler._build_attributes(**kwargs)


class TestAttributeCaps:
    def test_anti_vacuity_a_normal_call_emits_the_expected_fields(self):
        """If the helper stops emitting these, the caps below pass vacuously."""
        attrs = _attrs()
        missing = set(_CAPPED) - set(attrs)
        assert not missing, f"expected fields absent from a normal call: {missing}"

    def test_normal_values_are_not_truncated(self):
        """The caps must be generous enough that real values pass through whole,
        or this guard would corrupt data to satisfy itself."""
        attrs = _attrs()
        assert attrs["vin"] == "1HGBH41JXMN000046"
        assert attrs["vehicleId"] == "VEH-0025"
        assert attrs["driverName"] == "Sam Carter"
        assert attrs["tenantId"] == "meridian"
        assert attrs["triageSessionId"] == "triage-1"
        assert attrs["voiceSessionId"] == "voice-1"
        assert attrs["driverId"] == "DRV-1"

    def test_each_field_is_capped_at_its_limit(self):
        """Mutation: drop any `[:N]` slice → that field's case FAILS."""
        for field, (kwarg, cap) in _CAPPED.items():
            attrs = _attrs(**{kwarg: "X" * (cap + 500)})
            assert len(attrs[field]) == cap, (
                f"{field} must be capped at {cap}; got {len(attrs[field])}. "
                f"Connect caps the whole attribute map at 32KB, so an unbounded "
                f"value is a per-request denial of the escalation."
            )

    def test_every_attribute_is_bounded(self):
        """The structural half: no emitted field may grow without limit. Catches a
        field ADDED to _build_attributes without a cap, which per-field cases
        cannot — they only check fields this file already knows about."""
        long = "X" * 5000
        attrs = _attrs(
            vin=long, vehicle_id=long, reason=long, summary=long,
            triage_session_id=long, driver_id=long, display_name=long,
            voice_session_id=long, tenant_id=long, escalation_type=long,
        )
        unbounded = {
            k: len(v) for k, v in attrs.items()
            if k not in _CONSTANT_FIELDS and len(v) >= 5000
        }
        assert not unbounded, (
            f"these attributes are unbounded: {unbounded}. Add a `[:N]` cap in "
            "_build_attributes and a row to _CAPPED."
        )

    def test_total_attribute_map_stays_far_under_the_connect_limit(self):
        """The property the caps exist for, asserted end to end."""
        long = "X" * 5000
        attrs = _attrs(
            vin=long, vehicle_id=long, reason=long, summary=long,
            triage_session_id=long, driver_id=long, display_name=long,
            voice_session_id=long, tenant_id=long, escalation_type=long,
        )
        total = sum(len(k) + len(v) for k, v in attrs.items())
        assert total < 32_000, (
            f"attribute map is {total} bytes with every caller field oversized; "
            "Connect refuses above 32KB and the escalation fails"
        )
