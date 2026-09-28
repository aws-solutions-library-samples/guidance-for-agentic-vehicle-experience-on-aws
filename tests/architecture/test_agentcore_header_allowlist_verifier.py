"""Tests for `scripts/verify_agentcore_header_allowlist.py`.

The verifier exists because `.bedrock_agentcore.yaml` is gitignored, so the
allowlist that gates custom-header DELIVERY cannot be committed, and AgentCore
drops unlisted headers with **no error**. The verifier is the executable form of
a rule that was previously only a paragraph in `docs/tech.md`.

A guard is only worth its own anti-vacuity coverage: if `derive_read_headers`
silently matched nothing — a typo'd prefix, a walk that never recurses — then
every "covered" verdict would pass unconditionally and the guard would be worse
than nothing, because it would read as protection. This session already produced
four such false-CAUGHT guards, so the positive controls below are not ceremony.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify_agentcore_header_allowlist.py"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="verifier script absent")


def _load():
    spec = importlib.util.spec_from_file_location("_verify_allowlist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_verify_allowlist"] = mod
    spec.loader.exec_module(mod)
    return mod


V = _load()
PREFIX = "x-amzn-bedrock-agentcore-runtime-custom-"


# ---------------------------------------------------------------------------
# derive_read_headers — the half that must not silently match nothing
# ---------------------------------------------------------------------------


def _write(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    p = tmp_path / "entry.py"
    p.write_text(body)
    return p


def test_detects_a_bare_string_constant(tmp_path):
    src = f'USER_TOKEN_HEADER = "{PREFIX}user-token"\n'
    assert V.derive_read_headers(_write(tmp_path, src)) == {
        "USER_TOKEN_HEADER": [f"{PREFIX}user-token"]
    }


def test_detects_a_tuple_of_accepted_casings(tmp_path):
    src = (
        "VEHICLE_ID_QUERY_PARAMS = (\n"
        f'    "{PREFIX}vehicleid",\n'
        f'    "{PREFIX}vehicle-id",\n'
        f'    "{PREFIX}vehicle_id",\n'
        ")\n"
    )
    out = V.derive_read_headers(_write(tmp_path, src))
    assert list(out) == ["VEHICLE_ID_QUERY_PARAMS"]
    assert len(out["VEHICLE_ID_QUERY_PARAMS"]) == 3


def test_ignores_unrelated_constants(tmp_path):
    """A scanner that matched everything would be as useless as one matching nothing."""
    src = (
        'INPUT_SAMPLE_RATE = "16000"\n'
        'SOME_TABLE = "vsa-staging-acquire-catalog"\n'
        'OTHER = ("a", "b")\n'
    )
    assert V.derive_read_headers(_write(tmp_path, src)) == {}


def test_casing_is_normalised_so_a_camelcase_literal_still_matches(tmp_path):
    src = f'H = "{PREFIX.upper()}AvxFindingId"\n'
    out = V.derive_read_headers(_write(tmp_path, src))
    assert out["H"] == [f"{PREFIX}avxfindingid"]


def test_unparseable_entrypoint_raises_rather_than_returning_empty(tmp_path):
    """Returning {} on a syntax error would report 'nothing read' = vacuously clean."""
    with pytest.raises(V.CheckError):
        V.derive_read_headers(_write(tmp_path, "def broken( :\n"))


def test_missing_entrypoint_raises(tmp_path):
    with pytest.raises(V.CheckError):
        V.derive_read_headers(tmp_path / "nope.py")


def test_real_entrypoint_yields_a_plausible_inventory():
    """Positive control against the REAL file.

    Pins a floor rather than an exact count so ordinary additions don't fail it,
    and names the groups that must be found — if the scanner breaks, this is the
    test that notices against production source rather than a fixture.
    """
    out = V.derive_read_headers(REPO / "agents" / "supervisor" / "bidi_app.py")
    assert len(out) >= 8, f"implausibly few header groups derived: {sorted(out)}"
    for expected in (
        "USER_TOKEN_HEADER",
        "TENANT_QUERY_PARAM",
        "VIN_QUERY_PARAM",
        "VEHICLE_ID_QUERY_PARAMS",
        "AVX_FINDING_ID_QUERY_PARAMS",
    ):
        assert expected in out, f"{expected} not derived from the real entrypoint"
    for casings in out.values():
        assert casings, "a group was recorded with no casings"
        assert all(c.startswith(PREFIX) for c in casings)


# ---------------------------------------------------------------------------
# allowlist_for
# ---------------------------------------------------------------------------


def test_allowlist_is_read_for_the_named_runtime():
    cfg = {"agents": {"r1": {"request_header_configuration": {
        "requestHeaderAllowlist": ["X-A", "X-B"]}}}}
    assert V.allowlist_for(cfg, "r1") == ["X-A", "X-B"]


def test_unknown_runtime_raises_and_names_what_is_present():
    cfg = {"agents": {"r1": {}, "r2": {}}}
    with pytest.raises(V.CheckError) as exc:
        V.allowlist_for(cfg, "nope")
    assert "r1" in str(exc.value) and "r2" in str(exc.value)


def test_null_header_configuration_raises_with_the_websocket_explanation():
    """A text/HTTP runtime cannot take custom headers — a category error, not a gap.

    Returning [] here would make every read header look like a violation and
    train the reader to ignore the guard.
    """
    cfg = {"agents": {"text": {"request_header_configuration": None}}}
    with pytest.raises(V.CheckError) as exc:
        V.allowlist_for(cfg, "text")
    assert "WebSocket" in str(exc.value)


def test_absent_allowlist_key_is_empty_not_an_error():
    cfg = {"agents": {"r": {"request_header_configuration": {}}}}
    assert V.allowlist_for(cfg, "r") == []


# ---------------------------------------------------------------------------
# Coverage classification — matching must be case-insensitive and any-casing
# ---------------------------------------------------------------------------


def test_a_group_is_covered_when_any_single_casing_matches():
    """The wire carries ONE spelling; the server accepts three. Any match covers."""
    allowed = {f"{PREFIX}vehicleid"}
    casings = [f"{PREFIX}vehicleid", f"{PREFIX}vehicle-id", f"{PREFIX}vehicle_id"]
    assert any(c in allowed for c in casings)


def test_a_group_is_uncovered_when_no_casing_matches():
    allowed = {f"{PREFIX}vehicleid"}
    casings = [f"{PREFIX}latitude"]
    assert not any(c in allowed for c in casings)


# ---------------------------------------------------------------------------
# The declared-gap ratchet
# ---------------------------------------------------------------------------


def test_every_read_group_has_a_wire_name():
    """A read group with no declared wire name cannot be allowlisted at all.

    This is the test that forces the one-line `_WIRE_NAMES` declaration when
    someone adds a header. Without it, a new header would derive cleanly, fail
    the coverage check, and the deploy-time reconcile would have no spelling to
    add — so the only fix path would be hand-editing a gitignored file on every
    machine, which is the failure mode this whole guard exists to remove.
    """
    read = V.derive_read_headers(REPO / "agents" / "supervisor" / "bidi_app.py")
    missing = [n for n in read if n not in V._WIRE_NAMES]
    assert not missing, (
        f"no wire name declared for {missing}. Add the camelCase spelling the "
        "CLIENT sends to _WIRE_NAMES — it cannot be derived from the lowercased "
        "literal, and guessing yields an allowlist entry that matches nothing."
    )


def test_wire_names_do_not_carry_the_prefix():
    """The prefix is added by the consumer; embedding it would double it."""
    for name, wire in V._WIRE_NAMES.items():
        assert not wire.lower().startswith(PREFIX), f"{name}'s wire name includes the prefix"
        assert wire, f"{name} has an empty wire name"


def test_wire_name_lowercases_into_one_of_the_groups_accepted_casings():
    """The declared spelling must actually be one the server accepts.

    Catches the subtle version of a typo: a wire name that looks right but whose
    lowercased form is not among the casings the code checks, producing an
    allowlist entry that is delivered and then ignored.
    """
    read = V.derive_read_headers(REPO / "agents" / "supervisor" / "bidi_app.py")
    for name, casings in read.items():
        wire = V._WIRE_NAMES.get(name)
        if not wire:
            continue  # covered by test_every_read_group_has_a_wire_name
        delivered = (PREFIX + wire).lower()
        assert delivered in casings, (
            f"{name}: wire name {wire!r} delivers as {delivered!r}, which is not "
            f"among the casings the code accepts {casings}. The entry would be "
            "allowlisted and then ignored."
        )


def test_known_gaps_are_documented_with_a_real_reason():
    """An exemption without a reason is indistinguishable from an oversight."""
    assert V._KNOWN_GAPS, "the gap list is empty — if that is real, delete this test"
    for name, reason in V._KNOWN_GAPS.items():
        assert name.isupper(), f"{name} should be a module constant name"
        assert len(reason) > 60, f"{name}'s reason is too short to be a rationale"


def test_every_known_gap_is_a_header_group_the_code_actually_reads():
    """Anti-vacuity: a gap naming a non-existent constant would exempt nothing.

    Without this, a typo in the gap list would silently turn a real violation
    back into an undeclared one — or worse, permanently exempt a name that never
    appears, which reads as coverage.
    """
    read = V.derive_read_headers(REPO / "agents" / "supervisor" / "bidi_app.py")
    for name in V._KNOWN_GAPS:
        assert name in read, (
            f"_KNOWN_GAPS declares {name}, which the entrypoint does not read. "
            "Either it was renamed (update the gap list) or the exemption is dead."
        )


def test_the_real_repo_state_has_no_undeclared_gaps():
    """End-to-end against the real repo — the assertion the deploy gate makes.

    Skipped rather than failed when the config is absent: it is gitignored and
    per-developer, so its absence is an environment fact, not a defect. The
    script itself exits 2 in that case, which is what stops a deploy.
    """
    try:
        result = V.check("vsa_supervisor_bidi_staging")
    except V.CheckError as exc:
        pytest.skip(f"cannot check in this environment: {exc}")
    assert not result["undeclared_gaps"], (
        "headers are read but not allowlisted, so AgentCore will drop them "
        f"silently: {result['undeclared_gaps']}"
    )
    assert not result["stale_gap_declarations"], (
        "these are allowlisted but still declared as known gaps: "
        f"{result['stale_gap_declarations']}"
    )
