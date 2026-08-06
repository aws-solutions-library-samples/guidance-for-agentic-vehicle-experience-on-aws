"""Contract test: Swift `TenantConfig` fields vs this Lambda's `_PUBLIC_FIELDS`.

WHY THIS EXISTS
---------------
On 2026-07-29 the iOS client had declared `TenantConfig.acquire: AcquireConfig?`
since the Acquire spec shipped, but `_PUBLIC_FIELDS` here was never widened to
match. The field was therefore ALWAYS absent from the response, and every
tenant-configurable Acquire/Discover value — currency symbol, category label,
Discover greetings, primed prompts, plants, delivery cadence — silently fell
back to a hardcoded Swift default.

Silent is the operative word: an absent optional decodes cleanly in Swift, so
there was no error, no log line, and no way to notice from the client. The
entire tenant-configuration surface those specs designed was inert.

`_PUBLIC_FIELDS` is a denylist-by-omission — adding a client-visible config
block needs a coordinated change in two repos, and nothing linked them. This
test is that link.

APPROACH
--------
Source-parses the Swift struct rather than calling anything, so it needs no AWS
credentials, no network and no simulator. Skips when the CMS repo is not a
sibling checkout (same assumption `scripts/update-staging-xcconfig.py` makes).

Direction of the assertion matters: every field the CLIENT declares must be
deliverable. The reverse is fine — the Lambda may allowlist fields the current
client does not read yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CVX_ROOT = Path(__file__).resolve().parents[3]
_CMS_ROOT = _CVX_ROOT.parent / "connected-mobility-guidance-on-aws"
_HANDLER = Path(__file__).resolve().parents[1] / "handler.py"
_SWIFT_MODELS = _CMS_ROOT / "clients/ios/VSACompanion/Api/Models.swift"

# Declared by the client but deliberately NOT served by this endpoint.
# Every entry needs a reason — this is the escape hatch, so keep it honest.
_INTENTIONALLY_NOT_SERVED: dict[str, str] = {}


def _require_files() -> None:
    if not _SWIFT_MODELS.exists():
        pytest.skip(
            f"CMS repo not present at {_CMS_ROOT} — cross-repo contract test skipped"
        )


def _public_fields() -> set[str]:
    """Parse `_PUBLIC_FIELDS` out of the handler source."""
    src = _HANDLER.read_text()
    m = re.search(r"_PUBLIC_FIELDS\s*=\s*\{(.*?)\}", src, re.DOTALL)
    assert m, "_PUBLIC_FIELDS not found in handler.py"
    body = m.group(1)
    # Strip comments so a field name mentioned in prose is not counted.
    body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', body))


def _swift_tenant_config_fields() -> set[str]:
    """Top-level stored properties of the Swift `TenantConfig` struct.

    Only the outermost declaration block is considered — nested types
    (`Branding`, `Greeting`, …) describe the SHAPE of a field that is already
    covered by its parent's name, so their properties are irrelevant here.
    """
    src = _SWIFT_MODELS.read_text()
    m = re.search(r"struct\s+TenantConfig\s*:[^{]*\{(.*)", src, re.DOTALL)
    assert m, "struct TenantConfig not found in Models.swift"
    tail = m.group(1)

    fields: set[str] = set()
    depth = 0
    for raw in tail.splitlines():
        line = raw.split("//", 1)[0]
        if depth == 0:
            hit = re.match(r"\s*(?:let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if hit:
                fields.add(hit.group(1))
        depth += line.count("{") - line.count("}")
        if depth < 0:      # closed the struct itself
            break
    return fields


class TestTenantConfigContract:
    def test_every_client_field_is_deliverable(self):
        """The exact regression that made `acquire` inert."""
        _require_files()
        declared = _swift_tenant_config_fields()
        served = _public_fields()
        missing = declared - served - set(_INTENTIONALLY_NOT_SERVED)
        assert not missing, (
            f"Swift TenantConfig declares {sorted(missing)} but "
            f"_PUBLIC_FIELDS does not serve them, so they will ALWAYS be absent "
            f"from the response and the client will silently fall back to "
            f"hardcoded defaults. Add them to _PUBLIC_FIELDS, or record them in "
            f"_INTENTIONALLY_NOT_SERVED with a reason. "
            f"Served: {sorted(served)}"
        )

    def test_acquire_specifically_is_served(self):
        """Pin the specific field, not just the general rule."""
        assert "acquire" in _public_fields(), (
            "`acquire` must remain in _PUBLIC_FIELDS. Removing it silently "
            "disables all tenant-configurable Acquire/Discover values."
        )

    def test_parser_found_a_plausible_struct(self):
        """Guard against the regexes silently matching nothing.

        A contract test that parses zero fields would pass forever while
        checking nothing — the failure mode this whole file exists to prevent.
        """
        _require_files()
        declared = _swift_tenant_config_fields()
        assert len(declared) >= 5, f"parsed implausibly few fields: {declared}"
        for expected in ("tenantId", "displayName", "segment", "branding"):
            assert expected in declared, (
                f"expected {expected!r} among parsed TenantConfig fields; "
                f"got {sorted(declared)} — the parser is probably broken"
            )
