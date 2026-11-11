"""The seeders must not move a tenant's `current` pointer off an operator-pinned version.

`make deploy-staging` runs `seed-staging`, and both tenant seeders used to `put_item` the
`(tenantId, "current")` row unconditionally with their own version (`1.0.0`). Every tenant in
staging is pinned by hand to a later DB-only version (meridian `1.1.1`, the rest `1.1.0`), so
one deploy silently reverted all six — including meridian's `autoOpenHumanChatOnP0=false`.
See issues/2026-10-01-tenant-seed-repoints-current/.

These tests run against moto's DynamoDB so the ConditionExpressions are evaluated by a real
condition engine, not by a fake written alongside the code it checks.

Mutation ledger (run 2026-10-01, all CAUGHT): S1 unconditional repoint over a pin (the shipped
bug); S2 repoint when the pin is semver-older than the seed; S3/S4 create/repair without a
condition; S5 dangling pointer left in place; S6/S7 either seeder back to a direct put; S8
non-conditional put errors swallowed as a race; S9 empty-string activeVersion handled as
absent; S10 printed activation command unconditional; S11/S12 the no-attribute and
empty-string fills made unconditional (review cycle 2).

Run:
    pytest scripts/tests/test_seed_tenant_pointer.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

import tenant_pointer as tp  # noqa: E402

TABLE = "vsa-test-tenant-config"
REGION = "us-west-2"


def _load(script: str, modname: str):
    """Import a hyphenated seed script with neutral env/argv (it validates both at import)."""
    os.environ.setdefault("STAGE", "staging")
    os.environ.setdefault("AWS_REGION", REGION)
    path = _REPO / "scripts" / script
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    saved = sys.argv
    sys.argv = [str(path)]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    return mod


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "tenantId", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenantId", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


def _pointer(t, tenant="meridian"):
    item = t.get_item(Key={"tenantId": tenant, "version": "current"}).get("Item")
    return item and item.get("activeVersion")


def _put_version(t, version, tenant="meridian", **extra):
    t.put_item(Item={"tenantId": tenant, "version": version, **extra})


def _quiet(_msg):
    pass


# --------------------------------------------------------------------------
# The property: an operator pin to an existing version survives a seed.
# --------------------------------------------------------------------------

def test_operator_pinned_version_is_preserved(table):
    _put_version(table, "1.0.0")
    _put_version(table, "1.1.1")
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "1.1.1"})

    outcome = tp.set_current_pointer(table, "meridian", "1.0.0", log=_quiet)

    assert outcome == tp.PRESERVED
    assert _pointer(table) == "1.1.1", (
        "The seed moved meridian off 1.1.1. This is the deploy that turns "
        "autoOpenHumanChatOnP0 back on."
    )


def test_pin_preserved_even_when_seed_version_is_newer(table):
    # Guards the tempting "repoint if older than the seed" rule: on the next seed bump it
    # would move meridian off 1.2.1 and drop the P0 callback.
    _put_version(table, "1.2.1")
    _put_version(table, "2.0.0")
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "1.2.1"})

    outcome = tp.set_current_pointer(table, "meridian", "2.0.0", log=_quiet)

    assert outcome == tp.PRESERVED
    assert _pointer(table) == "1.2.1"


def test_preserved_notice_names_both_versions_and_the_activation_command(table):
    _put_version(table, "1.1.1")
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "1.1.1"})
    lines: list[str] = []

    tp.set_current_pointer(
        table, "meridian", "1.0.0", table_name=TABLE, region=REGION, log=lines.append
    )

    text = "\n".join(lines)
    assert "1.1.1" in text and "1.0.0" in text
    assert "NOT activated" in text
    assert f"--table-name {TABLE}" in text and f"--region {REGION}" in text


# --------------------------------------------------------------------------
# The seed still works on a fresh account and repairs an unusable pointer.
# --------------------------------------------------------------------------

def test_absent_pointer_is_created_at_seeded_version(table):
    _put_version(table, "1.0.0")

    outcome = tp.set_current_pointer(table, "meridian", "1.0.0", log=_quiet)

    assert outcome == tp.CREATED
    assert _pointer(table) == "1.0.0"


def test_pointer_row_without_active_version_is_filled(table):
    table.put_item(Item={"tenantId": "meridian", "version": "current"})

    outcome = tp.set_current_pointer(table, "meridian", "1.0.0", log=_quiet)

    assert outcome == tp.CREATED
    assert _pointer(table) == "1.0.0"


def test_dangling_pointer_is_repaired(table):
    _put_version(table, "1.0.0")
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "9.9.9"})

    outcome = tp.set_current_pointer(table, "meridian", "1.0.0", log=_quiet)

    assert outcome == tp.REPAIRED
    assert _pointer(table) == "1.0.0"


def test_pointer_already_at_seeded_version_is_untouched(table):
    _put_version(table, "1.0.0")
    table.put_item(Item={
        "tenantId": "meridian", "version": "current", "activeVersion": "1.0.0", "note": "keep",
    })

    outcome = tp.set_current_pointer(table, "meridian", "1.0.0", log=_quiet)

    assert outcome == tp.UNCHANGED
    item = table.get_item(Key={"tenantId": "meridian", "version": "current"})["Item"]
    assert item.get("note") == "keep", "an UNCHANGED outcome must not rewrite the row"


# --------------------------------------------------------------------------
# Races: a concurrent operator write between read and write wins.
# --------------------------------------------------------------------------

class _FlipBeforePut:
    """Wraps a real table; an operator pins 1.1.1 just before the seed's put lands."""

    def __init__(self, real):
        self._real = real

    def get_item(self, **kw):
        return self._real.get_item(**kw)

    def put_item(self, **kw):
        _put_version(self._real, "1.1.1")
        self._real.put_item(
            Item={"tenantId": "meridian", "version": "current", "activeVersion": "1.1.1"}
        )
        return self._real.put_item(**kw)


def test_create_loses_to_concurrent_operator_pin(table):
    outcome = tp.set_current_pointer(_FlipBeforePut(table), "meridian", "1.0.0", log=_quiet)
    assert outcome == tp.LOST_RACE
    assert _pointer(table) == "1.1.1"


def test_repair_loses_to_concurrent_operator_pin(table):
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "9.9.9"})
    outcome = tp.set_current_pointer(_FlipBeforePut(table), "meridian", "1.0.0", log=_quiet)
    assert outcome == tp.LOST_RACE
    assert _pointer(table) == "1.1.1"


@pytest.mark.parametrize("pointer_row", [
    {"tenantId": "meridian", "version": "current"},                       # no activeVersion
    {"tenantId": "meridian", "version": "current", "activeVersion": ""},  # empty string
])
def test_fill_of_unusable_pointer_loses_to_concurrent_operator_pin(table, pointer_row):
    # Review cycle 2 Suggestion: the two "row exists but unusable" branches had no race test.
    table.put_item(Item=pointer_row)
    outcome = tp.set_current_pointer(_FlipBeforePut(table), "meridian", "1.0.0", log=_quiet)
    assert outcome == tp.LOST_RACE
    assert _pointer(table) == "1.1.1"


def test_non_conditional_errors_propagate(table):
    class _Broken:
        def get_item(self, **_kw):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetItem")

    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        tp.set_current_pointer(_Broken(), "meridian", "1.0.0", log=_quiet)


class _PutFails:
    """Real reads; every put raises a non-conditional error (throttle, AccessDenied)."""

    def __init__(self, real, code):
        self._real, self._code = real, code

    def get_item(self, **kw):
        return self._real.get_item(**kw)

    def put_item(self, **_kw):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": self._code}}, "PutItem")


@pytest.mark.parametrize("code", ["ProvisionedThroughputExceededException", "AccessDeniedException"])
def test_put_error_on_absent_pointer_propagates(table, code):
    # Review 2026-10-01 Warning: a failed put reported as "written concurrently" exits 0 and
    # leaves no pointer, the same "reports success, did nothing" failure this issue fixes.
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        tp.set_current_pointer(_PutFails(table, code), "meridian", "1.0.0", log=_quiet)
    assert _pointer(table) is None


@pytest.mark.parametrize("code", ["ProvisionedThroughputExceededException", "AccessDeniedException"])
def test_put_error_on_dangling_pointer_propagates(table, code):
    from botocore.exceptions import ClientError
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "9.9.9"})
    with pytest.raises(ClientError):
        tp.set_current_pointer(_PutFails(table, code), "meridian", "1.0.0", log=_quiet)
    assert _pointer(table) == "9.9.9"


def test_empty_string_active_version_is_filled(table):
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": ""})

    outcome = tp.set_current_pointer(table, "meridian", "1.0.0", log=_quiet)

    assert outcome == tp.CREATED
    assert _pointer(table) == "1.0.0"


def test_printed_activation_command_is_conditional_on_the_pin(table):
    _put_version(table, "1.1.1")
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "1.1.1"})
    lines: list[str] = []

    tp.set_current_pointer(table, "meridian", "1.0.0", log=lines.append)

    text = "\n".join(lines)
    assert "--condition-expression 'activeVersion = :a'" in text
    assert '":a":{"S":"1.1.1"}' in text


# --------------------------------------------------------------------------
# Both seeders route through the helper — end to end against the same table.
# --------------------------------------------------------------------------

def test_seed_personas_put_preserves_meridian_pin(table, monkeypatch):
    sp = _load("seed-personas.py", "seed_personas_pointer_under_test")
    _put_version(table, "1.1.1")
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": "1.1.1"})

    sp._put(sp.MERIDIAN, table, connect_overrides={})

    assert _pointer(table) == "1.1.1"
    # The seed's own row is still written.
    assert table.get_item(Key={"tenantId": "meridian", "version": "1.0.0"}).get("Item")


def test_seed_personas_put_creates_pointer_on_fresh_table(table):
    sp = _load("seed-personas.py", "seed_personas_pointer_fresh_under_test")
    sp._put(sp.WAYPOINT_RENTALS, table, connect_overrides={})
    assert _pointer(table, "waypoint-rentals") == "1.0.0"


def test_seed_meridian_fleet_main_preserves_pin(table, monkeypatch):
    smf = _load("seed-meridian-fleet.py", "seed_meridian_fleet_pointer_under_test")
    _put_version(table, "1.1.0", tenant="meridian-fleet")
    table.put_item(
        Item={"tenantId": "meridian-fleet", "version": "current", "activeVersion": "1.1.0"}
    )

    class _Res:
        def Table(self, _name):  # noqa: N802 — boto3 API name
            return table

    monkeypatch.setattr(smf.boto3, "resource", lambda *_a, **_k: _Res())
    assert smf.main() == 0
    assert _pointer(table, "meridian-fleet") == "1.1.0"


def test_no_seeder_writes_the_current_row_directly():
    # Source guard: the unconditional put was a one-liner, easy to reintroduce. Any write to
    # the pointer must go through tenant_pointer.set_current_pointer. AST, not text, so a
    # single-quoted or reformatted literal is caught too; docstrings are not Constant nodes
    # equal to "current", so prose mentioning the pointer does not trip it.
    import ast

    for script in ("seed-personas.py", "seed-meridian-fleet.py"):
        tree = ast.parse((_REPO / "scripts" / script).read_text())
        literals = [n.lineno for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and n.value == "current"]
        assert not literals, f"{script} has a 'current' literal at line(s) {literals}"
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "set_current_pointer"]
        assert calls, f"{script} no longer calls set_current_pointer"



def test_printed_command_survives_a_hostile_stored_version(table):
    # Security review 2026-10-01 Suggestion: `activeVersion` is operator-written, so the
    # printed command must parse back to exactly the stored value, not run part of it.
    import json
    import shlex

    hostile = "1.1.1'; touch /tmp/pwned #\""
    _put_version(table, hostile)
    table.put_item(Item={"tenantId": "meridian", "version": "current", "activeVersion": hostile})
    lines: list[str] = []

    tp.set_current_pointer(table, "meridian", "1.0.0", table_name=TABLE, region=REGION,
                           log=lines.append)

    cmd = next(line for line in "\n".join(lines).splitlines() if "aws dynamodb put-item" in line)
    argv = shlex.split(cmd)
    assert argv[:3] == ["aws", "dynamodb", "put-item"]
    values = json.loads(argv[argv.index("--expression-attribute-values") + 1])
    assert values == {":a": {"S": hostile}}
    item = json.loads(argv[argv.index("--item") + 1])
    assert item == {"tenantId": {"S": "meridian"}, "version": {"S": "current"},
                    "activeVersion": {"S": "1.0.0"}}
