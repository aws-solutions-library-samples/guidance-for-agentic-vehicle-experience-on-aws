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
  3. Refresh the CMS iOS and DMS copies and their pins (the rebase pipeline in
     `scripts/check-contract-copies.py`'s docstring), then run
     `scripts/check-contract-copies.py` and expect exit 0.
  4. A commit message clearly naming the contract version bump.

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
# Rev: 2026-10-02 | v2.4 → v2.5, additive, all three pins rotated together (spec
# 2026-10-02-fi-action-queue-stage-a D3, D5, D8, D9). Finding: three FI agent ids, five
# `fleet.*` kinds, optional `subject_key`, `recommended_action`, `location`. Action:
# `cms` target, nine kinds (seven FI + the two retail kinds), optional `decided_by`,
# `decision`, audit-entry `guardrail`. OpenAPI: those fields plus the decline and
# approval-rules routes. Every pre-existing fixture validates unchanged. Old and new
# digests are in that spec's decisions.md. Re-cut the same day by its Fix Group 1
# (review Cycle 1): `fleet.rebalance.gap`, decided_by.kind `user`, minLength on the new
# ids, the audit `guardrail` if/then, and the route prose; nothing had been published.
# Re-cut again by its Fix Group 2 (review Cycle 2): `decision` only on FI decision
# Actions, PUT/DELETE approval-rules predicates, decline-reason and vehicle_vin prose.
# OpenAPI re-cut once more by Fix Group 3 (review Cycle 3): PATCH/dismiss 409, the
# /findings/me and /actions/owner exclusions, finding_key and created_by prose.
# Fix Group 4 (user direction): audit-entry failure_reason and error_status (spec D14).
# Fix Group 5 (review FG4-contract): the failure_reason vocabulary as a pattern, its
# if/then restrictions, and no `nullable` on the three v2.5 audit fields.
# Security Fix Group 1 (security review Cycle 1): x-scope-predicate on approve,
# decline, PATCH and dismiss, and the order-of-checks prose. OpenAPI only.
# Fix Group G2-2: the rules PUT 409 (`rule_changed_concurrently`) and the location rule.
# Git SHA at freeze-time commit: 88988a1af1fbed32dac634eccbc54f7390b815a8
# ---------------------------------------------------------------------------
FREEZE_CHECKSUMS: dict[str, str] = {
    # Rotated 2026-10-08 (was 3ec978f5…) for v2.7: spec 2026-10-05-fi-fleet-health-stage-d
    # task 3.5 (D14). Finding: two new agent_ids (agent-fi-fleet-health, agent-fi-quality),
    # three new finding_kinds (fleet.health.service, quality.investigation, quality.issue),
    # population_id scope field + oneOf branch, fleet.health.service p0 disclosure_txt allOf
    # guard. Additive; all v2.6 fixtures still validate. Old: 3ec978f5…
    # Rotated 2026-10-08 (G3F1.5 C1): added population_id: {not: {}} to each of the five
    # existing scope oneOf branches so {vin,population_id} etc. no longer validate. Old: 43a08265…
    "FINDING_SCHEMA": "3102e2a21fd2bb7df9873f4405c2b217a1e2a485770234cfa2f7dbecf2aad811",
    # Rotated 2026-10-08 (was 96c9cd0e…) for v2.7: spec 2026-10-05-fi-fleet-health-stage-d
    # task 3.5 (D14). Action: adp target_system, four new action_kinds (book_fleet_service,
    # record_engineering_decision, issue_recall, issue_software_campaign), engineer decided_by
    # kind, G5 guardrail, G5 failure_reason sentence + ADP in all target-name groups in
    # failure_reason pattern. Additive; all v2.6 fixtures still validate. Old: 96c9cd0e…
    # Rotated 2026-10-23 (was 8c0ab0ec…) for v2.8: one optional audit-entry field,
    # target_reason (the target's own refusal sentence, contract § 10b), plus one allOf rule
    # admitting it only on declined/target_refused with a 4xx error_status and no guardrail.
    # Additive; every v2.7 Action still validates. See
    # issues/2026-10-23-executor-drops-cms-refusal-reason/decisions.md.
    # Rotated 2026-11-08 (was 72ac1c50…) for v2.9: decided_by.kind += simulated_fleet_operator
    # (spec 2026-11-02-fi-ground-book-return D10, task 11.1 commit fb4893cf, which changed the
    # schema without rotating this pin; found and fixed by prompt 343). Additive; every v2.8
    # Action still validates. The CMS iOS and DMS copies are now behind (check-contract-copies.py).
    "ACTION_SCHEMA": "0f35a2d4a49f2b10dcf9a4aabb64545372f3d0a6b9e7689776b44171742b70d7",
    # Rotated 2026-09-28 (was f0167902…, frozen at 998d1aa). T5.1 added
    # /findings/me + CallerFindingListResponse + the S16 next_token warning:
    # 86 insertions, 0 deletions. Additive HTTP-surface documentation only —
    # both schema pins above are unchanged, so no data shape moved. See
    # .kiro/specs/2026-09-25-avx-own-vehicle-findings/decisions.md and
    # issues/2026-09-28-openapi-contract-pin-not-rotated/.
    # Rotated 2026-10-02 (was 12bfb33f…) for v2.5: Stage A fields plus the decline and
    # approval-rules routes. See .kiro/specs/2026-10-02-fi-action-queue-stage-a/decisions.md.
    # Rotated 2026-10-05 (was dd3efbcb…) for v2.6: server-set parameters per kind (Stage C
    # D11) — the table in § 4 and the prose around it. Prose only; JSON Schema pins
    # unchanged. See .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/decisions.md.
    # Rotated 2026-10-08 (was 88658e2f…) for v2.7: spec 2026-10-05-fi-fleet-health-stage-d
    # task 3.5 (D14). OpenAPI Finding: agent_id (+2), finding_kind (+3), FindingScope
    # population_id, p0 disclosure_txt allOf guard. OpenAPI Action: target_system (+adp),
    # action_kind (+4), decided_by.kind (+engineer), guardrail (+G5), failure_reason pattern
    # (G5 sentence + ADP). ApproveFindingRequest target_system (+adp) and action_kind (+4).
    # Rotated 2026-10-18 (was 62a7c5a5…) for stage E Group 8 (spec
    # 2026-10-06-fi-rebalancing-stage-e task 8.3, U6; group8-notes G8-D6): the approval-rules
    # PUT names move_vehicles as auto-approvable and limited, its limit codes, the new 503
    # auto_approval_bounds_unavailable and a move_rule example; ApprovalRuleRequest's
    # action_kind and limit descriptions. HTTP-surface documentation only; both JSON Schema
    # pins unchanged (no Finding or Action field moved), so CMS iOS and DMS copies are
    # unaffected.
    # Rotated 2026-10-26 (was 390f202b…) for T1.6 (spec
    # 2026-10-26-fi-sweep-avx-batch). PATCH declined writes failure_kind and
    # failure_reason; note maxLength 2000 on all note properties; x-fi-ios-excluded
    # extension on listActionsByOwner and listActionsByOwnerFleet. JSON Schema
    # pins unchanged (no Finding or Action schema field moved).
    # Rotated 2026-10-27 (F1.1, spec 2026-10-26-fi-sweep-avx-batch review C1):
    # no contract version change. OpenAPI-only: x-fi-ios-excluded extension added to
    # listFindingsForCaller, listFindingsByVehicle, listFindingsByOwner,
    # listFindingsByOwnerFleet (W4 fix; D13 exclusion was already applied by handlers
    # but the extension was missing). JSON Schema pins unchanged.
    # Rotated 2026-10-27 (G2.3, spec 2026-10-26-fi-sweep-avx-batch Fix Group 2):
    # no contract version change. OpenAPI-only: x-fi-ios-excluded added to
    # listActionsByVehicle (sec S3 owner-branch filter); owner-caller exclusion note
    # on both listFindingsByVehicle and listActionsByVehicle (review c2 S7);
    # UpdateActionRequest.note description corrected to say validated-not-stored
    # (review c2 S6). JSON Schema pins unchanged.
    # Rotated 2026-11-08 (was 033f7b84…) for v2.9: the Action schema's decided_by.kind enum and
    # description gain simulated_fleet_operator, matching action.schema.json (11.1 left the
    # OpenAPI behind; test_openapi_decided_by_kinds_match_the_action_schema now pins the two).
    # Rotated 2026-11-08 (was f337ca35…, prompt 362): POST /actions/{action_id}/dismiss gains
    # its 503 dismiss_unavailable and the 200 text states the write order (feedback row before
    # `dismissed`, rows expire after 30 days; issues/2026-11-08-avx-dismiss-write-ordering/).
    # HTTP-surface documentation only; both JSON Schema pins unchanged, so CMS iOS and DMS
    # copies are unaffected.
    "OPENAPI": "21fe9ab3b6b7aaac576df798b88ae5a3526a4907828aac8ab74eaa59d88b98a3",
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
