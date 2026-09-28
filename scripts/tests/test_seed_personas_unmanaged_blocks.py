"""The seeder must not destroy top-level blocks written by other seeders.

`seed-personas.py` runs on every `make deploy-staging` and `_put` replaces the
whole tenant row. This script does not define `acquire`, so every deploy deleted
it — 15 leaf paths including all three `upgradeOffers` and the nine
`existingFinance` fields, written by CMS
`deployment/scripts/seed_tenant_acquire_offers.py`.

The symptom is silent by construction: iOS `UpgradeOfferBanner` hides itself when
`acquire.upgradeOffers` is absent and deliberately has no empty state, so the
offer simply stops appearing. Same shape as the Connect PLACEHOLDER regression in
test_seed_personas_connect_preserve.py — configured state reset by a deploy, with
nothing in the output naming the cause.

Note the ordering these tests pin: the carry-forward runs on the DDB-ready `item`
AFTER `_put`'s `json.dumps` round-trip. A block read back from DynamoDB holds
`Decimal` values, and `json.dumps` raises `TypeError` on those — so doing this
before the round-trip does not merely smell wrong, it crashes the seeder.

Run:
    pytest scripts/tests/test_seed_personas_unmanaged_blocks.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load():
    """Import seed-personas.py (hyphenated; needs STAGE/REGION and a neutral argv)."""
    os.environ.setdefault("STAGE", "staging")
    os.environ.setdefault("AWS_REGION", "us-west-2")
    path = _REPO / "scripts" / "seed-personas.py"
    spec = importlib.util.spec_from_file_location("seed_personas_unmanaged_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    saved = sys.argv
    sys.argv = [str(path)]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    return mod


SP = _load()


# A stored block shaped like the real one: nested maps, a list, and Decimal
# numbers — because Decimal is what DynamoDB actually hands back and is the
# thing that breaks a naive implementation.
STORED_ACQUIRE = {
    "catalogVersion": "meridian-staging-2026-08",
    "currency": "USD",
    "upgradeOffers": [
        {"offerId": "offer-a", "modelName": "Model A", "price": "$66,400"},
        {"offerId": "offer-b", "modelName": "Model B", "price": "$78,900"},
    ],
    "existingFinance": {
        "lender": "Test Financial",
        "monthsRemaining": Decimal("36"),
        "outstanding": "$34,600",
    },
}


class FakeTable:
    """Minimal DDB table double. Records put_item calls; serves one stored row."""

    def __init__(self, existing=None, raise_on_get=False):
        self._existing = existing
        self._raise = raise_on_get
        self.puts = []

    def get_item(self, Key=None, **_):  # noqa: N803
        if self._raise:
            raise RuntimeError("simulated DynamoDB failure")
        return {"Item": self._existing} if self._existing else {}

    def put_item(self, Item=None, **_):  # noqa: N803
        self.puts.append(Item)


def _row(**over):
    row = {"tenantId": "meridian", "version": "1.0.0"}
    row.update(over)
    return row


# --------------------------------------------------------------------------
# The property: a stored block this script does not define must survive.
# --------------------------------------------------------------------------

def test_stored_acquire_block_is_carried_forward():
    item = _row()
    table = FakeTable(existing=_row(acquire=STORED_ACQUIRE))
    SP._preserve_unmanaged_blocks(item, table)
    assert "acquire" in item, (
        "acquire was dropped. This is the deploy that deletes the iOS upgrade offer."
    )
    assert item["acquire"] == STORED_ACQUIRE


def test_all_offers_survive_not_just_the_block_key():
    # Guards a shallow implementation that creates the key but loses contents.
    item = _row()
    table = FakeTable(existing=_row(acquire=STORED_ACQUIRE))
    SP._preserve_unmanaged_blocks(item, table)
    assert len(item["acquire"]["upgradeOffers"]) == 2
    assert {o["offerId"] for o in item["acquire"]["upgradeOffers"]} == {"offer-a", "offer-b"}
    assert item["acquire"]["existingFinance"]["outstanding"] == "$34,600"


def test_decimal_values_survive_unconverted():
    # DynamoDB numbers come back as Decimal and must be handed straight back.
    item = _row()
    table = FakeTable(existing=_row(acquire=STORED_ACQUIRE))
    SP._preserve_unmanaged_blocks(item, table)
    assert item["acquire"]["existingFinance"]["monthsRemaining"] == Decimal("36")


# --------------------------------------------------------------------------
# Precedence: this script wins when it actually has an opinion.
# --------------------------------------------------------------------------

def test_a_value_this_script_defines_is_NOT_shadowed_by_the_stored_one():
    # Mirrors _apply_connect_config's principle: a stored value must never
    # shadow a deliberate update. If seed-personas.py ever starts defining
    # `acquire`, its value wins.
    mine = {"catalogVersion": "defined-by-this-script"}
    item = _row(acquire=mine)
    table = FakeTable(existing=_row(acquire=STORED_ACQUIRE))
    SP._preserve_unmanaged_blocks(item, table)
    assert item["acquire"] == mine


# --------------------------------------------------------------------------
# Fail-safe: never make things worse than writing as-is.
# --------------------------------------------------------------------------

def test_absent_stored_row_is_not_an_error():
    item = _row()
    SP._preserve_unmanaged_blocks(item, FakeTable(existing=None))
    assert "acquire" not in item  # nothing to carry; fresh account still seeds


def test_stored_row_without_the_block_is_not_an_error():
    item = _row()
    SP._preserve_unmanaged_blocks(item, FakeTable(existing=_row()))
    assert "acquire" not in item


def test_empty_stored_block_is_not_carried():
    # An empty map is not worth carrying and would look like a real block.
    item = _row()
    SP._preserve_unmanaged_blocks(item, FakeTable(existing=_row(acquire={})))
    assert "acquire" not in item


def test_ddb_read_failure_does_not_abort_the_seed():
    # Seeding the other 40-odd fields must still happen; same posture as
    # _resolve_connect_from_ssm returning {} on any failure.
    item = _row()
    SP._preserve_unmanaged_blocks(item, FakeTable(raise_on_get=True))
    assert "acquire" not in item


def test_it_reads_the_row_it_is_about_to_overwrite():
    # A wrong key would read some other tenant's row and carry ITS offers over.
    seen = {}

    class KeyCapturingTable(FakeTable):
        def get_item(self, Key=None, **_):  # noqa: N803
            seen.update(Key)
            return {"Item": _row(acquire=STORED_ACQUIRE)}

    SP._preserve_unmanaged_blocks(_row(), KeyCapturingTable())
    assert seen == {"tenantId": "meridian", "version": "1.0.0"}


# --------------------------------------------------------------------------
# End to end through _put, which is where the Decimal ordering bites.
# --------------------------------------------------------------------------

def test_put_writes_the_carried_block_and_does_not_raise_on_decimals():
    table = FakeTable(existing=_row(acquire=STORED_ACQUIRE))
    # Real config so jsonschema.validate passes; MERIDIAN defines no `acquire`.
    SP._put(SP.MERIDIAN, table, {})
    assert table.puts, "_put wrote nothing"
    written = table.puts[0]
    assert "acquire" in written, (
        "_put dropped the stored acquire block — the deploy-time regression is back"
    )
    assert len(written["acquire"]["upgradeOffers"]) == 2


def test_put_still_writes_every_block_this_script_owns():
    # The carry-forward must be additive, not a replacement for normal seeding.
    table = FakeTable(existing=_row(acquire=STORED_ACQUIRE))
    SP._put(SP.MERIDIAN, table, {})
    written = table.puts[0]
    for required in ("tenantId", "version", "branding", "channels", "escalation", "metadata"):
        assert required in written, f"_put lost {required}"
