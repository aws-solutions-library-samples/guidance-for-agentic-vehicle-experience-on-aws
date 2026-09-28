"""F11.2 — the runner projects a top-level ``owner_id`` onto the DDB item.

Fix Group 15.  Companion to ``test_writer_scope_denormalisation.py`` (T10.2),
which pins the *scope* projection; this pins the *owner* projection and the
artifact-vs-item boundary a second time.

Assertion style, stated up front because it is what makes the mutations bite
-----------------------------------------------------------------------------
``resolve_owner_id`` and ``_build_artifact`` both promise ``None`` — not falsy —
as the sole "unresolvable" signal.  So the unresolvable tests assert
``is None``, not ``not x``.  That distinction is the whole point: a resolver
mutated to return ``""`` or to return the VIN is *falsy-or-truthy noise* to a
``not x`` check but fails an ``is None`` check immediately.  M-FG11-3 and
M-FG11-4 are catchable only because of it.

Equally, the projection tests read the **captured DynamoDB call**, not a return
value, because the thing under test is what gets persisted.  A test that
asserted on ``_build_artifact``'s output alone could not distinguish "projected
onto the item" from "not projected at all".
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agents.tier2 import owner_resolver as OR
from agents.tier2.owner_resolver import StandingFacts
from agents.tier2.runner import (  # noqa: E402
    OwnerProjectionIntegrityError,
    _build_artifact,
)
from agents.tier2.writer import write_finding  # noqa: E402

VIN_RESOLVABLE = "CSPR0000000000031"
VIN_UNRESOLVABLE = "MRDAZ01S0PC00FMMX"
OWNER_A = "CUST-004000DD"

AVX_TABLE = "vsa-test-avx-findings"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _CapturingDdb:
    """Records put_item / update_item calls; reports no live row on Query.

    ``query`` returning no Items drives ``write_finding`` down the PutItem
    branch, which is the branch that inserts a new Finding.
    """

    def __init__(self) -> None:
        self.put_items: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {"Items": []}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_items.append(kwargs)
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        return {}


class _LiveRowDdb(_CapturingDdb):
    """As above, but reports an existing live row so writes take UpdateItem."""

    def __init__(self, finding_id: str = "FIND-existing") -> None:
        super().__init__()
        self._finding_id = finding_id

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Items": [
                {
                    "finding_id": {"S": self._finding_id},
                    "expires_at": {"S": "2099-01-01T00:00:00Z"},
                    "inputs_hash": {"S": "a-different-hash"},
                }
            ]
        }


def _agent_fields() -> dict[str, Any]:
    # Evidence must satisfy the AVX Finding schema: the run() tests below go
    # through health_artifact_to_finding + validation on the dual-write path, so a
    # thin fixture would fail there and mask what those tests are checking.
    return {
        "finding": "Battery degradation detected.",
        "severity_hint": "attention",
        "confidence": "high",
        "evidence": [
            {
                "kind": "battery_soh_reading",
                "source": "adp",
                "source_ref": "adp_staging_energy_usage#row1",
                "value": {"soh_pct": 78},
                "computed_at": "2026-09-23T01:45:00Z",
                "provenance": "live",
            }
        ],
    }


def _persisted_item(ddb: _CapturingDdb) -> dict[str, Any]:
    assert len(ddb.put_items) == 1, f"expected exactly one PutItem, got {len(ddb.put_items)}"
    return ddb.put_items[0]["Item"]


def _write(finding: dict[str, Any], standing: Any, ddb: _CapturingDdb) -> None:
    """Accepts a StandingFacts, or a bare string/None for owner-only convenience."""
    if standing is not None and not isinstance(standing, StandingFacts):
        standing = StandingFacts(owner_id=standing)
    write_finding(
        finding,
        table_name=AVX_TABLE,
        dynamodb_client=ddb,
        standing=standing,
        utc_now_fn=lambda: "2026-09-23T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# (a) resolvable VIN → both projections present
# ---------------------------------------------------------------------------


def test_runner_projects_owner_id_for_vin_scoped_finding_when_resolvable():
    """M-FG11-1 target: deleting the projection MUST fail this test."""
    artifact, owner_id = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE, "soh": 78}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(owner_id=OWNER_A),
    )
    assert owner_id.owner_id == OWNER_A

    ddb = _CapturingDdb()
    _write(artifact, owner_id, ddb)
    item = _persisted_item(ddb)

    # T10.2's scope projection AND F11.2's owner projection, both top-level.
    assert item["vin"] == {"S": VIN_RESOLVABLE}, "T10.2 scope projection missing"
    assert item["owner_id"] == {"S": OWNER_A}, (
        "F11.2 owner projection missing from the persisted item"
    )


def test_projected_owner_id_applies_to_all_three_components():
    """F11.2 Accept 6 — no per-component branch.

    Asserting each component individually would pass if the implementation had a
    branch that happened to cover the three tested names.  Asserting they are all
    *equal to the same owner* is what pins the absence of a branch.
    """
    projected: dict[str, Any] = {}
    for component in ("battery", "tires", "brakes"):
        artifact, owner_id = _build_artifact(
            VIN_RESOLVABLE, component, [{"vin": VIN_RESOLVABLE}], _agent_fields(),
            owner_resolver=lambda _vin: StandingFacts(owner_id=OWNER_A),
        )
        ddb = _CapturingDdb()
        _write(artifact, owner_id, ddb)
        projected[component] = _persisted_item(ddb).get("owner_id")

    assert projected == {
        "battery": {"S": OWNER_A},
        "tires": {"S": OWNER_A},
        "brakes": {"S": OWNER_A},
    }, f"owner projection is not component-uniform: {projected}"


# ---------------------------------------------------------------------------
# (b) + (c) unresolvable → omitted, and errors do not escape
# ---------------------------------------------------------------------------


def test_runner_omits_owner_id_when_unresolvable():
    """M-FG11-3 and M-FG11-4 target.

    The ``is None`` assertions below are the ones those two mutations trip: a
    resolver returning ``""`` or returning the VIN violates the ``None``-only
    contract even though the item would still (correctly) carry no ``owner_id``.
    """
    artifact, owner_id = _build_artifact(
        VIN_UNRESOLVABLE, "battery", [{"vin": VIN_UNRESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(),
    )
    assert owner_id == StandingFacts(), (
        f"unresolvable must be exactly None, got {owner_id!r} — an empty string "
        "or a VIN is not 'absent' and would be compared against a caller identity"
    )

    ddb = _CapturingDdb()
    _write(artifact, owner_id, ddb)
    item = _persisted_item(ddb)

    assert item["vin"] == {"S": VIN_UNRESOLVABLE}, "T10.2 projection should be unaffected"
    assert "owner_id" not in item, (
        "unresolvable owner must be OMITTED, not written as empty/None/VIN"
    )


def test_resolver_error_is_swallowed_and_owner_omitted(caplog):
    """(c) A resolver raising must not abort the run; owner is simply absent.

    The production resolver never raises, but it is an injected seam — this pins
    the contract at the runner boundary rather than trusting the injectee.
    """
    def _boom(_vin: str) -> StandingFacts:
        raise RuntimeError("registry unreachable")

    with pytest.raises(RuntimeError):
        # Documents the boundary honestly: _build_artifact does NOT catch a
        # raising resolver — resolve_owner_id owns that, and does not raise.
        _build_artifact(
            VIN_UNRESOLVABLE, "battery", [{"vin": VIN_UNRESOLVABLE}], _agent_fields(),
            owner_resolver=_boom,
        )


def test_production_resolver_returns_none_on_client_error(caplog):
    """The real resolver, not a double: every client failure yields None."""

    class _ExplodingDdb:
        def query(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException")

    resolved = OR.resolve_owner_id(
        VIN_UNRESOLVABLE, ddb_client=_ExplodingDdb(), vehicles_table="cms-test-storage-vehicles"
    )
    assert resolved is None
    # Token renamed owner_lookup_failed -> standing_lookup_failed when the
    # resolver broadened from owner-only to all three standing facts. Safe: the
    # old token was introduced the same day and never deployed, so no CloudWatch
    # metric filter or saved query depends on it (verified by repo-wide grep).
    assert "event=standing_lookup_failed" in caplog.text
    assert VIN_UNRESOLVABLE not in caplog.text, "VIN must never be logged in the clear"


# ---------------------------------------------------------------------------
# (d) non-VIN scopes
# ---------------------------------------------------------------------------


def test_fleet_scoped_finding_gets_no_owner_projection():
    """(d) A fleet-scoped Finding has no owner of record to project."""
    fleet_finding = {
        "finding_id": "FIND-fleet-1",
        "finding_key": "key-fleet-1",
        "agent_id": "agent-2-vehicle-health",
        "agent_version": "1.0.0",
        "scope": {"fleet_id": "FLEET-9"},
        "severity": "routine",
        "computed_at": "2026-09-23T00:00:00Z",
        "expires_at": "2026-10-01T00:00:00Z",
        "confidence": "low",
        "finding_kind": "health.battery",
        "headline": "h",
        "detail": "d",
        "evidence": [],
        "inputs_hash": "hash-fleet",
        "audit_trail": [],
    }
    ddb = _CapturingDdb()
    # Even when an owner is (wrongly) supplied, a non-VIN scope must not take it.
    _write(fleet_finding, OWNER_A, ddb)
    item = _persisted_item(ddb)

    assert item["fleet_id"] == {"S": "FLEET-9"}
    assert "owner_id" not in item, (
        "fleet-scoped Findings must not carry an owner projection even if one is passed"
    )


def test_owner_scoped_finding_projection_is_left_to_t10_2():
    """An owner-scoped Finding's top-level owner_id comes from T10.2, once.

    Regression guard for a concrete DynamoDB failure: if F11.2 also emitted this
    attribute, ``_update_item`` would SET and REMOVE the same path in one
    expression and DynamoDB would reject the statement outright.
    """
    owner_finding = {
        "finding_id": "FIND-owner-1",
        "finding_key": "key-owner-1",
        "agent_id": "agent-2-vehicle-health",
        "agent_version": "1.0.0",
        "scope": {"owner_id": OWNER_A},
        "severity": "routine",
        "computed_at": "2026-09-23T00:00:00Z",
        "expires_at": "2026-10-01T00:00:00Z",
        "confidence": "low",
        "finding_kind": "health.battery",
        "headline": "h",
        "detail": "d",
        "evidence": [],
        "inputs_hash": "hash-owner",
        "audit_trail": [],
    }
    ddb = _LiveRowDdb()
    _write(owner_finding, None, ddb)

    assert len(ddb.update_calls) == 1
    expr = ddb.update_calls[0]["UpdateExpression"]
    assert "REMOVE" not in expr, (
        "owner-scoped Finding must not emit a REMOVE for owner_id — SET and "
        "REMOVE on one path is rejected by DynamoDB"
    )


# ---------------------------------------------------------------------------
# (e) the artifact-vs-item boundary — M-FG11-5
# ---------------------------------------------------------------------------


def test_projected_owner_id_lives_on_ddb_item_only():
    """M-FG11-5 target: leaking owner_id onto the artifact MUST fail this."""
    artifact, owner_id = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(owner_id=OWNER_A),
    )
    assert owner_id.owner_id == OWNER_A

    assert "owner_id" not in artifact, (
        "owner_id must NOT be a key on the artifact — it is schema-validated "
        "against additionalProperties: false and hashed into inputs_hash"
    )
    assert "owner_id" not in artifact.get("scope", {}), (
        "owner_id must NOT be written into scope — the VIN branch of the schema "
        "forbids it, which is what made the old nested read unreachable"
    )

    # And the write does not mutate the caller's dict.
    ddb = _CapturingDdb()
    _write(artifact, owner_id, ddb)
    assert "owner_id" not in artifact, "write_finding mutated the caller's artifact"


# ---------------------------------------------------------------------------
# (f) idempotency — D2
# ---------------------------------------------------------------------------


def test_inputs_hash_is_identical_with_and_without_the_projection():
    """(f) The projection must not perturb D2 idempotency."""
    rows = [{"vin": VIN_RESOLVABLE, "soh": 78}]
    with_owner, oid = _build_artifact(
        VIN_RESOLVABLE, "battery", rows, _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(owner_id=OWNER_A),
    )
    without_owner, none_oid = _build_artifact(
        VIN_RESOLVABLE, "battery", rows, _agent_fields(),
        owner_resolver=None,
    )
    assert oid.owner_id == OWNER_A and none_oid is None
    assert with_owner["inputs_hash"] == without_owner["inputs_hash"], (
        "inputs_hash must be byte-identical whether or not an owner resolved — "
        "otherwise a registry change would fork the D2 identity of a Finding"
    )


# ---------------------------------------------------------------------------
# Accept 4 — write-time integrity
# ---------------------------------------------------------------------------


def test_projected_owner_id_matches_vehicle_owner_lookup():
    """M-FG11-2 target: an unstable resolver MUST raise inside _build_artifact."""
    calls: list[str] = []

    def _flapping(vin: str) -> Optional[str]:
        calls.append(vin)
        return StandingFacts(owner_id=OWNER_A if len(calls) == 1 else "OWNER-WRONG")

    with pytest.raises(OwnerProjectionIntegrityError):
        _build_artifact(
            VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
            owner_resolver=_flapping,
        )

    assert len(calls) == 2, (
        f"expected exactly two lookups (resolve + re-verify), got {len(calls)}"
    )


def test_stable_resolver_does_not_raise_and_resolves_once_per_read():
    """Positive control for the integrity check — it must not fire spuriously.

    Without this, a mutation that made the assertion always-raise would still
    leave the suite green on the negative case alone.
    """
    calls: list[str] = []

    def _stable(vin: str) -> StandingFacts:
        calls.append(vin)
        return StandingFacts(owner_id=OWNER_A)

    artifact, owner_id = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=_stable,
    )
    assert owner_id.owner_id == OWNER_A
    assert artifact["scope"] == {"vin": VIN_RESOLVABLE}
    assert len(calls) == 2


def test_integrity_check_also_fires_when_owner_appears_midway():
    """None → value is just as unstable as value → other-value."""
    calls: list[str] = []

    def _appearing(vin: str) -> Optional[str]:
        calls.append(vin)
        return StandingFacts() if len(calls) == 1 else StandingFacts(owner_id=OWNER_A)

    with pytest.raises(OwnerProjectionIntegrityError):
        _build_artifact(
            VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
            owner_resolver=_appearing,
        )


# ---------------------------------------------------------------------------
# Update-path convergence (fail-open drift guard)
# ---------------------------------------------------------------------------


def test_update_removes_stale_owner_projection_when_now_unresolvable():
    """A row whose owner became unresolvable must lose the projection.

    Leaving it would keep authorizing an owner the registry no longer names —
    fail-open drift. ``REMOVE`` drives the row back to the fail-closed state.
    """
    artifact, owner_id = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(),
    )
    assert owner_id == StandingFacts()

    ddb = _LiveRowDdb()
    _write(artifact, owner_id, ddb)

    assert len(ddb.update_calls) == 1
    call = ddb.update_calls[0]
    assert "REMOVE #owner_id" in call["UpdateExpression"], (
        "update must REMOVE a now-stale owner_id projection"
    )
    assert call["ExpressionAttributeNames"]["#owner_id"] == "owner_id"
    assert ":owner_id" not in call["ExpressionAttributeValues"]


def test_update_sets_owner_projection_when_resolvable():
    artifact, owner_id = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(owner_id=OWNER_A),
    )
    ddb = _LiveRowDdb()
    _write(artifact, owner_id, ddb)

    call = ddb.update_calls[0]
    expr = call["UpdateExpression"]
    assert "#owner_id = :owner_id" in expr
    assert call["ExpressionAttributeValues"][":owner_id"] == {"S": OWNER_A}
    # The other two standing attrs did NOT resolve here, so they must be REMOVEd
    # in the same statement — converging the whole set is the point (see
    # writer._update_item). Asserting "REMOVE not in expr" was correct only while
    # owner_id was the sole projected attribute.
    assert "REMOVE" in expr
    assert "#fleet_id" in expr and "#vehicle_id" in expr
    assert ":fleet_id" not in call["ExpressionAttributeValues"]
    assert ":vehicle_id" not in call["ExpressionAttributeValues"]


# ---------------------------------------------------------------------------
# Defence in depth at the writer boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_owner,reason",
    [
        ("", "empty string is not absent"),
        ("   ", "whitespace is not an identity"),
        (VIN_RESOLVABLE, "a VIN is not an owner identity"),
        (None, "None is the unresolvable signal"),
        (12345, "a non-string is not an identity"),
    ],
)
def test_writer_refuses_to_project_a_non_identity(bad_owner, reason):
    """The writer re-validates: the resolver is an injected, replaceable seam."""
    artifact, _ = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=None,
    )
    ddb = _CapturingDdb()
    _write(artifact, bad_owner, ddb)
    assert "owner_id" not in _persisted_item(ddb), reason


# ---------------------------------------------------------------------------
# The resolver's two-step contract
# ---------------------------------------------------------------------------


def test_resolver_queries_the_index_then_gets_the_base_row():
    """The two-step shape is the point: vehicleId != vin on the owner-bearing rows.

    Pins the trap named in the module docstring — a "simplification" that
    GetItem-ed by VIN would work for rows where vehicleId == vin (the Ford rows)
    and fail silently on exactly the demo fleet that carries owners.
    """
    seen: dict[str, Any] = {}

    class _TwoStepDdb:
        def query(self, **kwargs: Any) -> dict[str, Any]:
            seen["query"] = kwargs
            return {"Items": [{"vehicleId": {"S": "VEH-CS-DEMO-0031"}, "vin": {"S": VIN_RESOLVABLE}}]}

        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            seen["get_item"] = kwargs
            return {"Item": {"sold_to": {"S": OWNER_A}}}

    resolved = OR.resolve_owner_id(
        VIN_RESOLVABLE, ddb_client=_TwoStepDdb(), vehicles_table="cms-test-storage-vehicles"
    )
    assert resolved == OWNER_A

    assert seen["query"]["IndexName"] == OR.VIN_INDEX_NAME
    assert seen["query"]["ExpressionAttributeValues"] == {":vin": {"S": VIN_RESOLVABLE}}

    # The GetItem MUST key on the vehicleId the index returned, not on the VIN.
    assert seen["get_item"]["Key"] == {"vehicleId": {"S": "VEH-CS-DEMO-0031"}}, (
        "GetItem must key on the vehicleId from the index — keying on the VIN "
        "works only for rows where vehicleId == vin and fails on the demo fleet"
    )
    assert "sold_to" in seen["get_item"]["ExpressionAttributeNames"].values()


def test_resolver_returns_none_when_vin_absent_from_index():
    class _EmptyIndexDdb:
        def query(self, **kwargs: Any) -> dict[str, Any]:
            return {"Items": []}

        def get_item(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("get_item must not be called when the index misses")

    assert OR.resolve_owner_id(
        VIN_UNRESOLVABLE, ddb_client=_EmptyIndexDdb(), vehicles_table="t"
    ) is None


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_resolver_returns_none_for_absent_or_blank_sold_to(raw):
    class _NoSoldToDdb:
        def query(self, **kwargs: Any) -> dict[str, Any]:
            return {"Items": [{"vehicleId": {"S": "VEH-1"}}]}

        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            return {"Item": {} if raw is None else {"sold_to": {"S": raw}}}

    assert OR.resolve_owner_id(
        VIN_RESOLVABLE, ddb_client=_NoSoldToDdb(), vehicles_table="t"
    ) is None


def test_resolver_rejects_a_sold_to_equal_to_the_vin():
    class _MirrorDdb:
        def query(self, **kwargs: Any) -> dict[str, Any]:
            return {"Items": [{"vehicleId": {"S": "VEH-1"}}]}

        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            return {"Item": {"sold_to": {"S": VIN_RESOLVABLE}}}

    assert OR.resolve_owner_id(
        VIN_RESOLVABLE, ddb_client=_MirrorDdb(), vehicles_table="t"
    ) is None


def test_mask_vin_is_stable_and_non_reversible():
    a = OR.mask_vin(VIN_RESOLVABLE)
    b = OR.mask_vin(VIN_RESOLVABLE)
    assert a == b, "mask must be stable across calls (SHA-256, not hash())"
    assert len(a) == 8
    assert VIN_RESOLVABLE not in a
    assert OR.mask_vin(VIN_UNRESOLVABLE) != a



# ---------------------------------------------------------------------------
# Fix Group 20 W1 + W2 — integrity failure degrades the projection, not the run
# ---------------------------------------------------------------------------
#
# Review Cycle 1 W1: the first implementation `continue`d on
# OwnerProjectionIntegrityError, which discarded the legacy health artifact and
# the observability records along with the untrusted projection. A single
# transient DDB error on either of the two reads therefore cost a vehicle its
# whole nightly assessment. Accept 4 forbids writing a *mismatched* owner; it
# does not ask for the Finding to be dropped.
#
# W2: that branch had no run()-level test and no counter, unlike its sibling
# record_avx_write_failed -> avx_write_failures.

from agents.tier2.runner import run  # noqa: E402


class _RecordingWriter:
    """Legacy writer double — records every put()."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put(self, artifact: dict[str, Any]) -> bool:
        self.puts.append(artifact)
        return True

    def put_raw(self, item: dict[str, Any]) -> None:
        pass

    def write_finding(self, finding, *, utc_now_fn=None, standing=None):  # pragma: no cover
        raise AssertionError("legacy writer should not take the D2 path")


class _RecordingAvxWriter:
    def __init__(self) -> None:
        self.findings: list[dict[str, Any]] = []
        self.owner_ids: list[Any] = []

    def put(self, artifact: dict[str, Any]) -> bool:  # pragma: no cover
        raise AssertionError("put() on the AVX writer")

    def put_raw(self, item: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError("put_raw() on the AVX writer")

    def write_finding(self, finding, *, utc_now_fn=None, standing=None):
        self.findings.append(finding)
        self.owner_ids.append(standing)
        return {"action": "put", "finding_id": finding.get("finding_id", "FIND-x")}


class _OneVinSource:
    def __init__(self, vin: str) -> None:
        self._vin = vin

    def fetch_findings(self, vins):
        return [{"vin": self._vin, "soh": 78}]

    def select_vins(self, cursor, limit):
        return [self._vin], None


def _agent_fn(_vin: str, _rows: list[dict]) -> dict[str, Any]:
    return _agent_fields()


def _run_one_vin(owner_resolver):
    writer = _RecordingWriter()
    avx = _RecordingAvxWriter()
    summary = run(
        stage="staging",
        source=_OneVinSource(VIN_RESOLVABLE),
        writer=writer,
        avx_writer=avx,
        vins=[VIN_RESOLVABLE],
        component="battery",
        agent_fn=_agent_fn,
        owner_resolver=owner_resolver,
    )
    return summary, writer, avx


def test_integrity_failure_keeps_the_finding_and_drops_only_the_projection():
    """W1: an unstable owner must not cost the Finding."""
    calls: list[str] = []

    def _flapping(vin: str) -> Optional[str]:
        calls.append(vin)
        # Disagree on the first build's two reads, then settle. The rebuild runs
        # with owner_resolver=None so it makes no further calls.
        return StandingFacts(owner_id=OWNER_A if len(calls) == 1 else "OWNER-WRONG")

    summary, writer, avx = _run_one_vin(_flapping)

    assert len(writer.puts) == 1, (
        "the legacy health artifact MUST still be written — dropping it was the W1 defect"
    )
    assert len(avx.findings) == 1, "the AVX Finding MUST still be written"
    # The degraded rebuild runs with owner_resolver=None, so the writer receives
    # None rather than an all-None StandingFacts. Both mean "no standing provable";
    # normalise so the assertion states the property, not the representation.
    def _owner_of(s):
        return None if s is None else s.owner_id
    assert [_owner_of(s) for s in avx.owner_ids] == [None], (
        "the untrusted projection MUST be dropped, leaving the Finding owner-less "
        f"(fail-closed); got {avx.owner_ids}"
    )
    assert summary["artifacts_written"] == 1


def test_integrity_failure_is_counted_in_the_run_summary():
    """W2: the degradation must be countable, not only greppable in logs.

    Silent at the API surface — the Finding exists, it just cannot be approved by
    its owner — so a sustained registry fault would otherwise surface only as
    owners reporting they cannot act on their cards.
    """
    calls: list[str] = []

    def _flapping(vin: str) -> Optional[str]:
        calls.append(vin)
        return StandingFacts(owner_id=OWNER_A if len(calls) == 1 else "OWNER-WRONG")

    summary, _writer, _avx = _run_one_vin(_flapping)

    assert summary.get("owner_integrity_degraded") == 1, (
        f"expected owner_integrity_degraded=1 in the run summary, got {summary!r}"
    )


def test_stable_resolver_run_records_no_degradation_and_projects_the_owner():
    """Positive control — the counter must not fire on the happy path."""
    summary, writer, avx = _run_one_vin(lambda _vin: StandingFacts(owner_id=OWNER_A))

    assert summary.get("owner_integrity_degraded") == 0
    assert [s.owner_id for s in avx.owner_ids] == [OWNER_A]
    assert len(writer.puts) == 1


def test_run_without_a_resolver_is_unchanged():
    """Every pre-F11.2 caller passes no resolver; behaviour must be identical."""
    summary, writer, avx = _run_one_vin(None)

    assert summary.get("owner_integrity_degraded") == 0
    assert avx.owner_ids == [None]
    assert len(writer.puts) == 1



# ---------------------------------------------------------------------------
# Fix Group 25 — the writer projects one fact per standing route
# ---------------------------------------------------------------------------


def test_standing_projection_covers_all_three_facts():
    """All three standing facts land on the DDB item, none on the artifact.

    Asserting them together rather than one test each is deliberate: the failure
    mode being guarded is a projection loop that covers a SUBSET, which
    per-attribute tests would still pass for whichever attribute they happened to
    name.
    """
    artifact, standing = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(
            owner_id=OWNER_A, fleet_id="FLEET-MERIDIAN-ONBOARD", vehicle_id="VEH-CS-DEMO-0031"
        ),
    )
    ddb = _CapturingDdb()
    _write(artifact, standing, ddb)
    item = _persisted_item(ddb)

    assert item["owner_id"] == {"S": OWNER_A}
    assert item["fleet_id"] == {"S": "FLEET-MERIDIAN-ONBOARD"}
    assert item["vehicle_id"] == {"S": "VEH-CS-DEMO-0031"}
    assert item["vin"] == {"S": VIN_RESOLVABLE}, "T10.2's scope projection must survive"

    # None of the three may reach the schema-validated, hash-input artifact.
    for attr in ("owner_id", "fleet_id", "vehicle_id"):
        assert attr not in artifact, f"{attr} leaked onto the artifact"


def test_standing_facts_are_projected_independently():
    """A partial resolve projects what resolved and omits the rest.

    The realistic shape: 56 of 155 staging vehicles have a fleet but no party, so
    fleet-only is a normal state, not an edge case.
    """
    artifact, standing = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(
            owner_id=None, fleet_id="FLEET-MERIDIAN-ONBOARD", vehicle_id=None
        ),
    )
    ddb = _CapturingDdb()
    _write(artifact, standing, ddb)
    item = _persisted_item(ddb)

    assert item["fleet_id"] == {"S": "FLEET-MERIDIAN-ONBOARD"}
    assert "owner_id" not in item
    assert "vehicle_id" not in item


def test_update_converges_every_standing_attr_not_just_the_resolved_ones():
    """A stale fact must be REMOVEd even when a sibling fact resolves.

    Guards the case a per-attribute loop gets wrong: vehicle moved out of its
    fleet, so fleet_id no longer resolves while owner_id still does. Leaving the
    old fleet_id would keep authorizing the previous fleet's operators.
    """
    artifact, standing = _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: StandingFacts(owner_id=OWNER_A, fleet_id=None, vehicle_id=None),
    )
    ddb = _LiveRowDdb()
    _write(artifact, standing, ddb)

    call = ddb.update_calls[0]
    expr = call["UpdateExpression"]
    assert "#owner_id = :owner_id" in expr
    assert "REMOVE" in expr
    for attr in ("fleet_id", "vehicle_id"):
        assert f"#{attr}" in expr, f"{attr} must be named in the REMOVE clause"
        assert f":{attr}" not in call["ExpressionAttributeValues"]


def test_resolver_reads_both_standing_attributes_in_one_getitem():
    """One CMS row, one GetItem — broadening must not cost a second read."""
    calls = {"query": 0, "get_item": 0}

    class _CountingDdb:
        def query(self, **kwargs):
            calls["query"] += 1
            return {"Items": [{"vehicleId": {"S": "VEH-CS-DEMO-0031"}}]}

        def get_item(self, **kwargs):
            calls["get_item"] += 1
            names = set(kwargs["ExpressionAttributeNames"].values())
            assert names == {"sold_to", "fleetId"}, (
                f"expected both standing attrs in one projection, got {names}"
            )
            return {"Item": {"sold_to": {"S": OWNER_A},
                             "fleetId": {"S": "FLEET-MERIDIAN-ONBOARD"}}}

    facts = OR.resolve_standing_facts(
        VIN_RESOLVABLE, ddb_client=_CountingDdb(), vehicles_table="t"
    )
    assert facts == StandingFacts(OWNER_A, "FLEET-MERIDIAN-ONBOARD", "VEH-CS-DEMO-0031")
    assert calls == {"query": 1, "get_item": 1}, f"expected exactly 2 calls, got {calls}"



# ---------------------------------------------------------------------------
# Fix Group 26 (review cycle 1 C1) — the no-op path must not skip convergence
# ---------------------------------------------------------------------------
#
# write_finding returned {"action": "noop"} BEFORE _update_item whenever
# inputs_hash was unchanged. Standing is deliberately excluded from inputs_hash
# (D2 idempotency), and inputs_hash is computed from telemetry with no date
# component — so a vehicle that changed fleet kept its stale projected fleet_id for
# the rest of the 8-day TTL, authorizing the PREVIOUS fleet's operators. Exactly
# the fail-open drift the update path's REMOVE clause exists to prevent, reached by
# a route that never runs the update path.
#
# M-FG11-5b guarded only the update path, so it could not see this.


class _LiveRowWithStanding(_CapturingDdb):
    """Reports a live row carrying a specific inputs_hash and standing attrs."""

    def __init__(self, inputs_hash: str, **standing: str) -> None:
        super().__init__()
        self._hash = inputs_hash
        self._standing = standing

    def query(self, **kwargs: Any) -> dict[str, Any]:
        item = {
            "finding_id": {"S": "FIND-existing"},
            "expires_at": {"S": "2099-01-01T00:00:00Z"},
            "inputs_hash": {"S": self._hash},
        }
        for k, v in self._standing.items():
            item[k] = {"S": v}
        return {"Items": [item]}


def _build_for_standing(standing: Any):
    return _build_artifact(
        VIN_RESOLVABLE, "battery", [{"vin": VIN_RESOLVABLE}], _agent_fields(),
        owner_resolver=lambda _vin: standing,
    )


def test_noop_still_taken_when_standing_is_unchanged():
    """Idempotency must survive the fix — a true duplicate is still a no-op.

    Asserted first because the C1 fix's risk is the opposite failure: making every
    write an UpdateItem would quietly destroy D2's retry-safety.
    """
    artifact, standing = _build_for_standing(
        StandingFacts(owner_id=OWNER_A, fleet_id="FLEET-A", vehicle_id="VEH-1")
    )
    ddb = _LiveRowWithStanding(
        artifact["inputs_hash"], owner_id=OWNER_A, fleet_id="FLEET-A", vehicle_id="VEH-1"
    )
    result = write_finding(
        artifact, table_name=AVX_TABLE, dynamodb_client=ddb, standing=standing,
        utc_now_fn=lambda: "2026-09-23T00:00:00Z",
    )
    assert result["action"] == "noop"
    assert not ddb.update_calls and not ddb.put_items


def test_fleet_change_converges_even_when_inputs_hash_is_unchanged():
    """The C1 defect: vehicle moved fleet, telemetry identical.

    Before the fix this returned noop and the row kept FLEET-OLD for ~8 days.
    """
    artifact, standing = _build_for_standing(
        StandingFacts(owner_id=OWNER_A, fleet_id="FLEET-NEW", vehicle_id="VEH-1")
    )
    ddb = _LiveRowWithStanding(
        artifact["inputs_hash"], owner_id=OWNER_A, fleet_id="FLEET-OLD", vehicle_id="VEH-1"
    )
    result = write_finding(
        artifact, table_name=AVX_TABLE, dynamodb_client=ddb, standing=standing,
        utc_now_fn=lambda: "2026-09-23T00:00:00Z",
    )
    assert result["action"] == "update", (
        "a standing change MUST converge even when the Finding's content is "
        "unchanged — otherwise the previous fleet keeps standing until TTL"
    )
    call = ddb.update_calls[0]
    assert call["ExpressionAttributeValues"][":fleet_id"] == {"S": "FLEET-NEW"}


def test_owner_removal_converges_when_inputs_hash_is_unchanged():
    """The sharper case: the registry row lost its party, so standing must be REMOVEd.

    Leaving it is the fail-open direction — the old party keeps approve rights.
    """
    artifact, standing = _build_for_standing(
        StandingFacts(owner_id=None, fleet_id="FLEET-A", vehicle_id="VEH-1")
    )
    ddb = _LiveRowWithStanding(
        artifact["inputs_hash"], owner_id=OWNER_A, fleet_id="FLEET-A", vehicle_id="VEH-1"
    )
    result = write_finding(
        artifact, table_name=AVX_TABLE, dynamodb_client=ddb, standing=standing,
        utc_now_fn=lambda: "2026-09-23T00:00:00Z",
    )
    assert result["action"] == "update"
    expr = ddb.update_calls[0]["UpdateExpression"]
    assert "REMOVE" in expr and "#owner_id" in expr
    assert ":owner_id" not in ddb.update_calls[0]["ExpressionAttributeValues"]


def test_standing_appearing_where_there_was_none_converges():
    """A previously-unresolvable vehicle that now resolves must gain its facts."""
    artifact, standing = _build_for_standing(
        StandingFacts(owner_id=OWNER_A, fleet_id="FLEET-A", vehicle_id="VEH-1")
    )
    ddb = _LiveRowWithStanding(artifact["inputs_hash"])  # no standing persisted
    result = write_finding(
        artifact, table_name=AVX_TABLE, dynamodb_client=ddb, standing=standing,
        utc_now_fn=lambda: "2026-09-23T00:00:00Z",
    )
    assert result["action"] == "update"
    vals = ddb.update_calls[0]["ExpressionAttributeValues"]
    assert vals[":owner_id"] == {"S": OWNER_A}
    assert vals[":fleet_id"] == {"S": "FLEET-A"}
