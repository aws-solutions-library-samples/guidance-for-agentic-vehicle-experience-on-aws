"""
Cross-spec byte-identical fixture pin for the AVX contract files.

PURPOSE
-------
This test is the tripwire against Spec 1b and Spec 2 forking the contract
without going through a deliberate freeze step. The three files pinned here are
the canonical source of truth for Findings, Actions, and the API surface:

  agents/tier2/schemas/finding.schema.json
  agents/tier2/schemas/action.schema.json
  docs/api/avx-findings-actions.openapi.yaml

ANY modification to these files — even adding a trailing space — will make this
test fail in red. That is the intended behavior. Updating the pin requires:
  1. A deliberate spec-level decision recorded in
     .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/decisions.md
  2. Running `python scripts/freeze_contract_checksums.py` (or equivalent)
     to regenerate the FREEZE_CHECKSUMS below.
  3. A commit message clearly naming the contract version bump.

FREEZE CHECKSUMS
----------------
Frozen: 2026-09-18 (contract v2.2-final — see "RE-FREEZE", "FIX GROUP 1", "FIX GROUP 2" below)
Git SHA at v1 freeze: 88988a1af1fbed32dac634eccbc54f7390b815a8
Git SHA at v2 re-freeze: c5033fff4033ef4bda9cc82bcb4023097c14185a
Spec: 2026-09-18-avx-tier2-ios-cards-and-actions Group 1

FIX GROUP 1 — review Cycle 1 Critical (OpenAPI only; v2 -> v2.1)
----------------------------------------------------------------
`components/parameters/StatusFilter` enumerated 6 of the 8 `Action.status` values,
omitting `dismissed` (D4) and `failed` (D5) — the two the addenda had just added.
`Action.status` and both `ActionAuditEntry` status fields were already correct, so
the frozen contract disagreed with itself, and the omission made exactly the two new
terminal states unfilterable through the API (a 400 on a legitimate `?status=failed`
query once API Gateway request validation is on).

Same defect class as G2/G3 below: the values existed in the model and the surface that
consumes them was never brought into parity. Fixed, and guarded by
`TestApiContractStatusEnumParity`, which asserts parity across EVERY status enum in the
document rather than spot-checking the one that drifted. Only the OpenAPI checksum
changed; the two JSON Schema pins are unchanged from v2.

FIX GROUP 2 — review Cycle 1 residue (all three files; v2.2 → v2.2-final)
---------------------------------------------------------------------------
The first v2.2 rotation (security review Cycle 1 W1/W2, OpenAPI-only) left four
contract-level findings open that touch all three frozen artifacts. They are bundled
here so the checksums rotate exactly ONCE as the true final v2.2 rather than a third
mid-flight rotation.

  C2  Action.updated_at required — spec.md line 340 mandates "a computed_at on
      Findings and an updated_at on Actions so the caller can surface staleness
      (parent PRD § M5)". The field existed in the description but was absent from
      both the JSON Schema required array and the Action properties. Group 8's iOS
      cards render staleness from this, so nullable/optional does not satisfy it.
      Added as required RFC 3339 date-time. Mirrors FindingListResponse.computed_at.
      All six action fixtures updated to carry updated_at set to the last audit_trail
      entry's timestamp (or created_at if no transitions). spec.md canonical Action
      block reconciled.

  W1  ApproveFindingRequest upgraded from flat-enum exclusion to oneOf — the flat
      enum (contract v2.1/v2.2 W1 fix) made ios-owner unrepresentable but did not
      annotate WHY or name the caller-role boundary machine-readably. Restructured
      to oneOf with a branch carrying the per-target description and enum. The test
      now asserts both the structural constraint (oneOf present) and the property
      (ios-owner absent from all branches). A flat-enum-only guard "asserts presence
      not the property" in the sense that a reviewer must read the prose to understand
      what ios-owner is and why it is excluded; the oneOf surfaces this in the schema.

  W2  District membership predicate — already closed in the prior v2.2 rotation
      (OpenAPI authorizer text updated on both district routes). No further change;
      the TestApiContractAuthorizationShape guard covers it.

  S1  Monetary finding_kind disclosure guard — finding_kind enum includes 5 monetary
      values (coverage.warranty, coverage.recall, energy.cost, loyalty.points,
      loyalty.expiry) but the frozen schema had no if/then requiring non-empty
      disclosure_txt. A Spec 1b Agent 3 Finding with e.g. finding_kind='energy.cost'
      and disclosure_txt absent would validate — a silent omission at the
      deterministic-seam boundary (~/.kiro/steering/agentic-tiers.md). Added:
      allOf with a second if/then branch in finding.schema.json. Added in the OpenAPI
      Finding schema too (if/then on the component schema). Spec 1a Agent 2 emits no
      monetary Findings; tests exercise via synthetic fixtures.

MUTATION VERIFICATION RECORD (v2.2-final, four properties, four mutations)
---------------------------------------------------------------------------
All four mutations were run, each failed its own named assertion, each reverted.
See decisions.md § "2026-09-18 — Contract v2.2 final re-freeze" for the full record.

  C2  updated_at removed from action.schema.json required[]
      -> FAILED test_action_without_updated_at_fails_validation (CAUGHT)
      Reverted; suite green.

  W1  ios-owner added back to the oneOf branch target_system enum
      -> FAILED test_approve_route_cannot_originate_ios_owner_actions (CAUGHT)
      AND test_approve_request_has_machine_readable_structure (CAUGHT)
      Reverted; suite green.

  S1  disclosure_txt removed from monetary fixture (finding_kind=energy.cost)
      -> FAILED test_monetary_finding_without_disclosure_txt_fails[energy.cost] (CAUGHT)
      Reverted; suite green.

  S1b disclosure_txt set to "" (empty) for monetary fixture
      -> FAILED test_monetary_finding_with_empty_disclosure_txt_fails[energy.cost] (CAUGHT)
      Reverted; suite green.

Old checksums (v2.2 pre-final):
  FINDING_SCHEMA: 374c4641d15c1fac1bc72593de4769bc5778c170ba588ecc46033eb0e249738c
  ACTION_SCHEMA:  6e8d5b6cd4705201fa1d416a0814684de20fda744440a9cb4cb09538009919de
  OPENAPI:        9ec09d3cd234920483ba60b432cbf30d4ca9d5d23eafac3c34059b1835f57ddd

RE-FREEZE (contract v2, 2026-09-18) — why the checksums changed once
--------------------------------------------------------------------
Contract v1 declared the Addenda D1/D4/D5 fields but did not ENFORCE the
properties that make them meaningful. Verification against spec.md found five
gaps; all five validated as ACCEPTED under v1 and are REJECTED under v2. The
enum values were present in v1 — the bindings were not, which is the
"asserts presence, not the property" failure mode in
~/.kiro/steering/testing.md.

  G1  D1  A `scope.fleet_id` Finding with `evidence: []` validated. The
          per-entry `vin` requirement sat under `items`, which is vacuously
          true on an empty array — so the one guarantee D1 exists to make
          (aggregation happens at WRITE time, with per-VIN attribution) was
          unenforced exactly when evidence was absent. Fixed: `minItems: 1`
          in the fleet-scoped `then` branch.

  G2  D5  `to_status: declined` + `failure_kind: executor_exhausted_retries`
          validated — i.e. "dealer said no" was indistinguishable from "we
          could not reach the dealer", which is the ENTIRE distinction D5
          exists to create. Fixed: `declined` now requires
          `failure_kind == target_refused`.

  G3  D5  The inverse — `to_status: failed` + `failure_kind: target_refused`
          validated. Fixed: `failed` constrains `failure_kind` to
          {executor_exhausted_retries, executor_terminal_error}.

  G4  D5  `to_status: failed` without `retry_count` validated, though spec.md
          couples retry_count to that transition and D5 names
          `test_failed_transition_records_retry_count_in_audit_trail`.
          Fixed: `retry_count` required when `to_status == failed`.
          NOTE (interpretation, flagged for the user): retry_count is NOT
          required on `declined`, because a terminal 4xx is defined as "no
          retry fired" and the shipped declined fixture omits it.

  G5  API 4 of the 8 `/actions/*` scope routes spec.md § "API contract"
          requires were absent (`owner-fleet`, `fleet`, `dealership`,
          `district`), while the document's OWN description asserted the three
          stub equivalents existed. `/actions/owner-fleet/{owner_id}` is Spec
          1a scope, not a stub — the `fleet_owner` persona approves
          fleet-scoped Findings and needs to list the resulting Actions.
          Fixed: 4 routes added; findings/actions scope surfaces now symmetric
          (6 + 6). `ios-owner` is deliberately NOT a separate route — it is
          served by `GET /actions/owner/{owner_id}`, as that route documents.

MUTATION VERIFICATION RECORD (per ~/.kiro/steering/testing.md § "Mutation testing")
---------------------
Two independent rounds, both on contract v2.

Round 1 — synthetic byte mutation, one file at a time (append a single space):
  finding.schema.json  -> FAILED on test_finding_schema_checksum  -> CAUGHT
  action.schema.json   -> FAILED on test_action_schema_checksum   -> CAUGHT
  openapi.yaml         -> FAILED on test_openapi_checksum         -> CAUGHT
  All three reverted; checksums restored byte-exact; 8/8 green after revert.
  Each mutation failed via its OWN intended assertion, not a neighbour's.

Round 2 — real semantic change (stronger evidence than Round 1, because the
  bytes changed for a reason rather than by whitespace): applying the G1-G4
  schema fixes above made exactly test_finding_schema_checksum and
  test_action_schema_checksum fail while test_openapi_checksum stayed green,
  which is correct — the OpenAPI had not yet been edited at that point.

Round 3 — property mutation, not byte mutation: each of G1-G5 was probed with
  a document that SHOULD be rejected, plus positive controls proving the
  shipped fixtures still validate. Under v1: 4 of 5 probes ACCEPTED (the gap).
  Under v2: 5 of 5 REJECTED, each via its intended constraint, with all
  positive controls still ACCEPTED.

See decisions.md § "2026-09-18 — Contract v2 re-freeze" for the full record.
"""

import hashlib
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Repository root and file paths
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent

FINDING_SCHEMA_PATH = _REPO_ROOT / "agents" / "tier2" / "schemas" / "finding.schema.json"
ACTION_SCHEMA_PATH = _REPO_ROOT / "agents" / "tier2" / "schemas" / "action.schema.json"
OPENAPI_PATH = _REPO_ROOT / "docs" / "api" / "avx-findings-actions.openapi.yaml"

# ---------------------------------------------------------------------------
# Freeze checksums — DO NOT EDIT without updating decisions.md first.
#
# These are SHA-256 hashes of the file bytes at freeze-time.
# Frozen: 2026-09-18 | contract v2.2-final (JSON Schemas unchanged since v2.2)
# OpenAPI rev: 2026-09-18 | Fix Group 5 — C1 minimal fix (top-level `required`
# deleted from `ApproveFindingRequest`; `oneOf` branch's `required` was already
# equivalent). JSON Schemas untouched — deliberate carve-out, per Fix Group 5
# in tasks.md and decisions.md § "Fix Group 5 — Cycle 2 C1: 0.7.1 vs 0.9.0
# validator disagreement".
# OpenAPI rev: 2026-09-24 | v2.2-final → v2.3, Fix Groups 35 + 36 (prose only,
# no schema object changed). Owner read routes: `sub` OR trusted
# `custom:customerId` must equal `owner_id` (D-G10-4). Approve: the executor's
# D5 result is what the row records, not "retried asynchronously" (D-G10-1), and
# the `dms` parameter rules (D-G10-2). One rotation for both groups. JSON Schema
# pins unchanged. Re-cut before any release of v2.3, by Fix Group 38 (review
# Cycle 2 N3): the approve text now states the re-approve 409 (D-G10-9) and the
# `dms` caller-group rule (D-G10-7), and the `book_dms_visit` example carries
# `dealer_id`. Still v2.3: nothing was published at the first cut. Re-cut again
# by Fix Group 39 (review Cycle 3 S3): approve success documented as 200, which the
# handler has always returned (was 201), and its example shows a real D5 result
# (in_progress, executor-authored audit entry, dealer_id + vehicle_vin).
# Rev: 2026-09-26 | v2.3 → v2.4, additive: `health.diagnostics` joins the
# `finding_kind` enum in finding.schema.json and the OpenAPI Finding schema (spec
# 2026-09-25-avx-own-vehicle-findings T4.1). Active fault codes outside tires,
# brakes and battery. iOS ships its `unknown` fallback and the new case before any
# producer writes the kind (that spec's decisions.md). Action schema unchanged.
# Git SHA at freeze-time commit: 88988a1af1fbed32dac634eccbc54f7390b815a8
# ---------------------------------------------------------------------------
FREEZE_CHECKSUMS: dict[str, str] = {
    "FINDING_SCHEMA": "855351e29743093cd819ef777827fcd0c98fac28e72ef0f7225239779acf20d9",
    "ACTION_SCHEMA": "510fdbc7940f90acdbce2f6accc63e6738f159d54b2dce1476132c3cde54f24d",
    "OPENAPI": "f0167902a9ce8f7f303bcd8a63ebb4679262dac19a901b26aad90841f8fdae2a",
}


def _sha256(path: pathlib.Path) -> str:
    """Return the hex SHA-256 digest of the file at *path*."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestContractFilesExist:
    """All three contract files must be present in the repository."""

    def test_finding_schema_exists(self) -> None:
        assert FINDING_SCHEMA_PATH.exists(), (
            f"Contract file missing: {FINDING_SCHEMA_PATH}\n"
            "Run the Spec 1a Group 1 'Freeze Finding + Action' task to create it."
        )

    def test_action_schema_exists(self) -> None:
        assert ACTION_SCHEMA_PATH.exists(), (
            f"Contract file missing: {ACTION_SCHEMA_PATH}\n"
            "Run the Spec 1a Group 1 'Freeze Finding + Action' task to create it."
        )

    def test_openapi_exists(self) -> None:
        assert OPENAPI_PATH.exists(), (
            f"Contract file missing: {OPENAPI_PATH}\n"
            "Run the Spec 1a Group 1 'Freeze API contract' task to create it."
        )


class TestContractByteIdentity:
    """
    The byte content of all three contract files must match the frozen checksums.

    Any drift — even a single whitespace change — fails this test. That is
    intentional: this is the tripwire that ensures Spec 1b and Spec 2 cannot
    fork the contract without a deliberate decision.

    To update the checksums after an intentional contract change:
      1. Record the decision in decisions.md
      2. Recompute: sha256sum agents/tier2/schemas/{finding,action}.schema.json
                             docs/api/avx-findings-actions.openapi.yaml
      3. Update FREEZE_CHECKSUMS above
      4. Document the mutation-verification run for the new checksums
    """

    def test_finding_schema_checksum(self) -> None:
        """finding.schema.json must be byte-identical to the frozen version."""
        actual = _sha256(FINDING_SCHEMA_PATH)
        expected = FREEZE_CHECKSUMS["FINDING_SCHEMA"]
        assert actual == expected, (
            f"finding.schema.json has drifted from the frozen contract.\n"
            f"  Expected SHA-256: {expected}\n"
            f"  Actual   SHA-256: {actual}\n\n"
            "This test is the tripwire against Spec 1b/Spec 2 forking the contract.\n"
            "If this is an intentional change, update decisions.md and FREEZE_CHECKSUMS."
        )

    def test_action_schema_checksum(self) -> None:
        """action.schema.json must be byte-identical to the frozen version."""
        actual = _sha256(ACTION_SCHEMA_PATH)
        expected = FREEZE_CHECKSUMS["ACTION_SCHEMA"]
        assert actual == expected, (
            f"action.schema.json has drifted from the frozen contract.\n"
            f"  Expected SHA-256: {expected}\n"
            f"  Actual   SHA-256: {actual}\n\n"
            "This test is the tripwire against Spec 1b/Spec 2 forking the contract.\n"
            "If this is an intentional change, update decisions.md and FREEZE_CHECKSUMS."
        )

    def test_openapi_checksum(self) -> None:
        """avx-findings-actions.openapi.yaml must be byte-identical to the frozen version."""
        actual = _sha256(OPENAPI_PATH)
        expected = FREEZE_CHECKSUMS["OPENAPI"]
        assert actual == expected, (
            f"avx-findings-actions.openapi.yaml has drifted from the frozen contract.\n"
            f"  Expected SHA-256: {expected}\n"
            f"  Actual   SHA-256: {actual}\n\n"
            "This test is the tripwire against Spec 1b/Spec 2 forking the contract.\n"
            "If this is an intentional change, update decisions.md and FREEZE_CHECKSUMS."
        )


class TestFreezeChecksumCompleteness:
    """
    Guard: FREEZE_CHECKSUMS must contain exactly the expected three keys.
    This prevents accidentally running with a partial or empty dict.
    """

    _EXPECTED_KEYS = {"FINDING_SCHEMA", "ACTION_SCHEMA", "OPENAPI"}

    def test_freeze_checksums_has_all_keys(self) -> None:
        assert set(FREEZE_CHECKSUMS.keys()) == self._EXPECTED_KEYS, (
            f"FREEZE_CHECKSUMS is incomplete. Expected keys: {self._EXPECTED_KEYS}\n"
            f"Actual keys: {set(FREEZE_CHECKSUMS.keys())}"
        )

    def test_freeze_checksums_no_empty_values(self) -> None:
        for key, value in FREEZE_CHECKSUMS.items():
            assert value and len(value) == 64, (
                f"FREEZE_CHECKSUMS[{key!r}] is not a valid SHA-256 hex digest: {value!r}\n"
                "SHA-256 hex digests are exactly 64 hex characters."
            )
