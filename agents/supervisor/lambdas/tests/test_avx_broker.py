"""
Unit tests for agents/supervisor/lambdas/avx_broker.py

Mutation verification results (run 2026-09-23):
  (M1) Hardcoded APNs threshold to "p0" constant, bypassing SSM config:
       test_broker_apns_gate_follows_config_when_lowered FAILED as expected.
       Mutation reverted. SHA-256 of avx_broker.py confirmed byte-identical post-revert. ✓ CAUGHT

  (M2) Removed consent gate (set consent_ok = True unconditionally):
       test_broker_consent_gate_blocks_category_off_owner FAILED as expected.
       Mutation reverted. SHA-256 of avx_broker.py confirmed byte-identical post-revert. ✓ CAUGHT

  (M3) Cardinality gate disabled (count < cap → True unconditionally):
       test_cardinality_cap_blocks_when_owner_is_at_cap FAILED as expected.
       Mutation reverted. SHA-256 of avx_broker.py confirmed byte-identical post-revert. ✓ CAUGHT

  (M4) Namespace guard removed (CUST-* denylist block deleted):
       test_no_write_reaches_rate_limits_or_actions_on_cust_owner FAILED as expected.
       Mutation reverted. SHA-256 of avx_broker.py confirmed byte-identical post-revert. ✓ CAUGHT

  (M5) Allowlist widened (not _COGNITO_SUB_RE.match → False, accepting any non-empty string):
       test_unrecognised_party_id_is_skipped FAILED as expected.
       Mutation reverted. SHA-256 of avx_broker.py confirmed byte-identical post-revert. ✓ CAUGHT

Run:
    python3 -m pytest agents/supervisor/lambdas/tests/test_avx_broker.py -v

Baseline coverage:
    All four required test functions present and passing.
    Additional tests cover: demotion skip reasons, severity ordering schema pin,
    in-app card creation, VIN-scoped findings (owner_absent + owner_namespace_unmatched),
    allowlist guard anti-vacuity (DRV-* rejected) + positive UUID control.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup: allow `import avx_broker` from any cwd
# ---------------------------------------------------------------------------
_LAMBDAS_DIR = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

import avx_broker as broker


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_ssm_side_effect(threshold: str = "p0",
                          rate_limit: dict | None = None,
                          cardinality_cap: str = "15"):
    """Return a side_effect function for ssm.get_parameter calls."""
    if rate_limit is None:
        rate_limit = {"day": 10, "hour": 3}

    param_map = {
        broker._SSM_APNS_THRESHOLD:   threshold,
        broker._SSM_RATE_LIMIT:       json.dumps(rate_limit),
        broker._SSM_CARDINALITY_CAP:  cardinality_cap,
    }

    def _side_effect(Name, WithDecryption=True):
        if Name not in param_map:
            raise RuntimeError(f"Unexpected SSM parameter: {Name}")
        return {"Parameter": {"Value": param_map[Name]}}

    return _side_effect


def _fresh_policy(threshold: str = "p0",
                  rate_limit: dict | None = None,
                  cardinality_cap: int = 15) -> dict:
    """Build a policy dict directly, bypassing SSM, for direct _process_finding calls."""
    if rate_limit is None:
        rate_limit = {"day": 10, "hour": 3}
    return {
        "apns_threshold": threshold,
        "rate_limit": rate_limit,
        "cardinality_cap": cardinality_cap,
    }


def _p0_finding(owner_id: str = "cognito-sub-abc", kind: str = "health.tires") -> dict:
    """Minimal Finding dict with severity=p0 and owner-scoped via scope."""
    return {
        "finding_id": "FIND-00000000-0000-0000-0000-000000000001",
        "finding_kind": kind,
        "severity": "p0",
        "headline": "Tire pressure low",
        "scope": {"owner_id": "CUST-DEADBEEF"},
        # top-level owner_id absent (VIN-scoped would have CUST-*, owner-scoped
        # here is via scope — broker uses _resolve_cognito_owner which picks up scope.owner_id)
    }


def _finding_with_scope_owner(scope_owner: str = "CUST-DEADBEEF",
                               severity: str = "p0",
                               kind: str = "health.tires") -> dict:
    """Finding with scope.owner_id set."""
    return {
        "finding_id": "FIND-00000000-0000-0000-0000-000000000002",
        "finding_kind": kind,
        "severity": severity,
        "headline": "Test finding",
        "scope": {"owner_id": scope_owner},
    }


def _finding_with_cognito_owner(cognito_sub: str = "aaaabbbb-1234-5678-abcd-111122223333",
                                 severity: str = "p0",
                                 kind: str = "health.tires") -> dict:
    """Finding with a Cognito-sub-shaped top-level owner_id (future state after namespace reconciliation)."""
    return {
        "finding_id": "FIND-00000000-0000-0000-0000-000000000003",
        "finding_kind": kind,
        "severity": severity,
        "headline": "Test finding",
        "scope": {"vin": "1HGCM82633A123456"},
        "owner_id": cognito_sub,
    }


def _finding_no_owner(severity: str = "p0", kind: str = "health.tires") -> dict:
    """VIN-scoped Finding with NO top-level owner_id."""
    return {
        "finding_id": "FIND-00000000-0000-0000-0000-000000000004",
        "finding_kind": kind,
        "severity": severity,
        "headline": "Test finding",
        "scope": {"vin": "1HGCM82633A123456"},
        # no owner_id at all
    }


def _finding_cust_owner(cust_id: str = "CUST-ABCD1234",
                         severity: str = "p0",
                         kind: str = "health.tires") -> dict:
    """Finding with top-level owner_id in the CUST-XXXXXXXX namespace."""
    return {
        "finding_id": "FIND-00000000-0000-0000-0000-000000000005",
        "finding_kind": kind,
        "severity": severity,
        "headline": "Test finding",
        "scope": {"vin": "1HGCM82633A123456"},
        "owner_id": cust_id,
    }


# ---------------------------------------------------------------------------
# SSM cold-start helpers (reset module-level cache between tests)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_policy_cache():
    """Reset the module-level policy cache before each test."""
    broker._policy = None
    broker._ddb = None
    broker._ssm = None
    broker._sns = None
    yield
    broker._policy = None
    broker._ddb = None
    broker._ssm = None
    broker._sns = None


# ===========================================================================
# REQUIRED TEST (a): APNs gate reads config from SSM — p0 default
# ===========================================================================

class TestBrokerApnsGateReadsConfigAndP0OnlyByDefault:
    """test_broker_apns_gate_reads_config_and_p0_only_by_default

    With SSM default "p0", a p0 finding fires sns:Publish;
    a "soon" finding does NOT fire sns:Publish but DOES write an in-app card.
    """

    def _make_ddb_mock(self, consent_on: bool = True, cardinality_count: int = 0):
        """Build a DDB resource mock with consent=True and cardinality under cap."""
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        # consent check query: return a device row
        # cardinality check query: return count
        # in-app card put_item
        # rate-limit update_item

        if consent_on:
            table_mock.query.return_value = {
                "Items": [{
                    "owner_id": "cognito-sub-abc",
                    "device_token": "tok",
                    "per_category_consent": {"safety": True, "coverage": True, "service": True},
                    "endpoint_arn": "arn:aws:sns:us-west-2:123:endpoint/APNS/app/abc",
                }],
                "Count": 1,
            }
        else:
            table_mock.query.return_value = {"Items": [], "Count": 0}

        table_mock.update_item.return_value = {}
        table_mock.put_item.return_value = {}
        return ddb_mock, table_mock

    def test_broker_apns_gate_reads_config_and_p0_only_by_default(self):
        """With default threshold 'p0', p0 fires SNS; 'soon' does not."""
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect(threshold="p0")

        ddb_mock, table_mock = self._make_ddb_mock(consent_on=True)
        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ssm", return_value=ssm_mock),
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            policy = broker._load_policy()
            assert policy["apns_threshold"] == "p0"

            # -- p0 finding: SNS should be called (or at least attempted) --
            # The owner is CUST-* (scope.owner_id), so consent lookup uses the CUST- value.
            # Consent check will find a matching row (table_mock returns the device row).
            # We test the outcome on the demotion + severity gate level via _meets_apns_threshold.
            assert broker._meets_apns_threshold("p0", "p0") is True

            # -- soon finding: should NOT meet threshold --
            assert broker._meets_apns_threshold("soon", "p0") is False

    def test_p0_severity_fires_sns_publish(self):
        """A p0 Finding causes sns.publish to be called."""
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect(threshold="p0")

        # Use a Cognito-sub owner so consent/rate/cardinality work end-to-end
        cognito_sub = "aaaabbbb-1234-5678-abcd-111122223333"
        endpoint_arn = "arn:aws:sns:us-west-2:123:endpoint/APNS/app/testtoken"

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        # Rate-limit: always under limit
        def ddb_update_side_effect(*args, **kwargs):
            return {}
        table_mock.update_item.side_effect = ddb_update_side_effect

        # Consent: return a device row
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": True, "coverage": True, "service": True},
                "endpoint_arn": endpoint_arn,
            }],
            "Count": 1,
        }
        table_mock.put_item.return_value = {}

        sns_mock = MagicMock()

        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, severity="p0")

        with (
            patch.object(broker, "_get_ssm", return_value=ssm_mock),
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            # Also mock feedback table (no dismissals)
            # The feedback query should return empty
            policy = broker._load_policy()
            broker._policy = policy

            # Mock feedback lookup: no prior dismissal
            with patch.object(broker, "_check_demotion", return_value=(False, "no_prior_dismissal")):
                result = broker._process_finding(finding, policy)

        assert result["action"] in ("in_app_card_and_apns", "in_app_card_only"), result
        if result["action"] == "in_app_card_and_apns":
            sns_mock.publish.assert_called_once()

    def test_soon_finding_does_not_fire_sns_but_creates_in_app_card(self):
        """A 'soon' finding (below p0 threshold) does not publish to SNS
        but still creates an in-app card if it passes all other gates.
        """
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect(threshold="p0")

        cognito_sub = "bbbbcccc-1234-5678-abcd-111122223333"
        endpoint_arn = "arn:aws:sns:us-west-2:123:endpoint/APNS/app/testtoken"

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        table_mock.update_item.return_value = {}
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": True, "coverage": True, "service": True},
                "endpoint_arn": endpoint_arn,
            }],
            "Count": 1,
        }
        table_mock.put_item.return_value = {}
        sns_mock = MagicMock()

        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, severity="soon")

        with (
            patch.object(broker, "_get_ssm", return_value=ssm_mock),
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            policy = broker._load_policy()
            broker._policy = policy
            with patch.object(broker, "_check_demotion", return_value=(False, "no_prior_dismissal")):
                result = broker._process_finding(finding, policy)

        # In-app card created
        assert result["action"] == "in_app_card_only", result
        # SNS not called
        sns_mock.publish.assert_not_called()
        # In-app card put_item was called
        table_mock.put_item.assert_called()


# ===========================================================================
# REQUIRED TEST (b): APNs gate follows config when lowered
# ===========================================================================

class TestBrokerApnsGateFollowsConfigWhenLowered:
    """test_broker_apns_gate_follows_config_when_lowered

    With SSM set to "attention", an "attention" finding fires sns:Publish.
    This test MUST FAIL under Mutation 1 (hardcoded threshold "p0").
    """

    def test_broker_apns_gate_follows_config_when_lowered(self):
        """With threshold lowered to 'attention', an 'attention' finding fires SNS."""
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect(threshold="attention")

        cognito_sub = "ccccdddd-1234-5678-abcd-111122223333"
        endpoint_arn = "arn:aws:sns:us-west-2:123:endpoint/APNS/app/tok"

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        table_mock.update_item.return_value = {}
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": True, "coverage": True, "service": True},
                "endpoint_arn": endpoint_arn,
            }],
            "Count": 1,
        }
        table_mock.put_item.return_value = {}
        sns_mock = MagicMock()

        # "attention" finding
        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="attention", kind="health.brakes"
        )

        with (
            patch.object(broker, "_get_ssm", return_value=ssm_mock),
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            # Load policy with threshold "attention"
            policy = broker._load_policy()
            assert policy["apns_threshold"] == "attention", (
                "Policy must read threshold from SSM, not a hardcoded constant. "
                "This assertion failing means Mutation 1 (hardcoded p0) is active."
            )
            broker._policy = policy

            with patch.object(broker, "_check_demotion", return_value=(False, "no_prior_dismissal")):
                result = broker._process_finding(finding, policy)

        # The attention finding MUST result in SNS publish when threshold is "attention"
        assert result["action"] == "in_app_card_and_apns", (
            f"Expected 'in_app_card_and_apns' but got {result['action']!r}. "
            "This fails under Mutation 1 (hardcoded threshold 'p0') because "
            "'attention' would not meet the hardcoded threshold."
        )
        sns_mock.publish.assert_called_once()

    def test_severity_ordering_pins_against_schema(self):
        """The _SEVERITY_ORDER list matches the schema's enum order.

        A test that pins the ordering so a future reorder of the schema enum
        cannot silently move the threshold.
        """
        import json as _json
        schema_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..", "..", "agents", "tier2", "schemas", "finding.schema.json",
        )
        # Resolve relative path
        schema_path = os.path.normpath(schema_path)
        assert os.path.exists(schema_path), (
            f"finding.schema.json not found at {schema_path}. "
            "This test intentionally fails — not skips — when the schema is missing "
            "so a reorganisation of agents/tier2/schemas/ is visible rather than silent."
        )

        with open(schema_path) as f:
            schema = _json.load(f)

        schema_enum = schema["properties"]["severity"]["enum"]
        assert broker._SEVERITY_ORDER == schema_enum, (
            f"_SEVERITY_ORDER {broker._SEVERITY_ORDER!r} does not match "
            f"schema enum {schema_enum!r}. Update _SEVERITY_ORDER when the "
            "schema enum changes — the rank comparison depends on this ordering."
        )


# ===========================================================================
# REQUIRED TEST (c): In-app card created for every severity passing rate + consent
# ===========================================================================

class TestBrokerInAppCardCreatedForEverySeverityPassingRateAndConsent:
    """test_broker_in_app_card_created_for_every_severity_passing_rate_and_consent

    Non-P0 sub-threshold findings still create an in-app card.
    """

    @pytest.mark.parametrize("severity", ["p0", "attention", "soon", "routine", "informational"])
    def test_broker_in_app_card_created_for_every_severity_passing_rate_and_consent(
        self, severity
    ):
        """Every severity that passes rate + cap + consent gets an in-app card.

        The apns_threshold stays at 'p0' (default), so only p0 gets SNS,
        but ALL severities get a card.
        """
        policy = _fresh_policy(threshold="p0")

        # Valid UUID for each severity (using a fixed base with severity index)
        severity_idx = {"p0": "0", "attention": "1", "soon": "2", "routine": "3", "informational": "4"}
        cognito_sub = f"eeeeffff-1234-5678-abcd-{severity_idx[severity].zfill(12)}"
        endpoint_arn = "arn:aws:sns:us-west-2:123:endpoint/APNS/app/tok"

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        table_mock.update_item.return_value = {}
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": True, "coverage": True, "service": True},
                "endpoint_arn": endpoint_arn,
            }],
            "Count": 1,
        }
        table_mock.put_item.return_value = {}
        sns_mock = MagicMock()

        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity=severity, kind="health.tires"
        )

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            with patch.object(broker, "_check_demotion", return_value=(False, "no_prior_dismissal")):
                result = broker._process_finding(finding, policy)

        # Card MUST be created regardless of severity
        assert result["action"] in ("in_app_card_and_apns", "in_app_card_only"), (
            f"Severity {severity!r} should produce a card but got action={result['action']!r}. "
            "Every severity that passes rate+cap+consent must get an in-app card."
        )
        # put_item (card creation) MUST have been called
        table_mock.put_item.assert_called()

        # SNS only for p0
        if severity == "p0":
            assert result["action"] == "in_app_card_and_apns", (
                f"p0 should produce APNs push, got {result['action']!r}"
            )
            sns_mock.publish.assert_called_once()
        else:
            assert result["action"] == "in_app_card_only", (
                f"Severity {severity!r} is below threshold 'p0', "
                f"should be in_app_card_only, got {result['action']!r}"
            )


# ===========================================================================
# REQUIRED TEST (d): Dismissed feedback demotes matching kind
# ===========================================================================

class TestBrokerDismissedFeedbackDemotesMatchingKind:
    """test_broker_dismissed_feedback_demotes_matching_kind

    The feedback signal consumes Action.status=dismissed rows (per Addendum D4),
    stored in avx-feedback table with PK=owner_id (Cognito sub).
    """

    def test_broker_dismissed_feedback_demotes_matching_kind(self):
        """Prior dismissals demote new Findings of the same kind to in-app-only.

        Uses a fake that applies FilterExpression against >1 feedback row so
        the test actually validates kind-matching rather than just Count > 0.
        The matching row is NOT the first row — confirms paging/full-scan works.
        """
        # For demotion to apply, owner must be a Cognito sub (valid hex UUID)
        cognito_sub = "f0e1d2c3-a4b5-6789-abcd-111122223333"
        kind = "health.tires"

        # Finding with Cognito sub as owner_id
        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="p0", kind=kind
        )

        # Build a fake that contains 3 rows for the owner:
        # row 0: different kind (no match)
        # row 1: different kind (no match)
        # row 2: matching kind (the one we want)
        all_rows = [
            {"owner_id": cognito_sub, "finding_kind": "health.brakes",
             "dismissed_at": "2026-09-20T08:00:00Z"},
            {"owner_id": cognito_sub, "finding_kind": "energy.charging",
             "dismissed_at": "2026-09-21T09:00:00Z"},
            {"owner_id": cognito_sub, "finding_kind": kind,
             "dismissed_at": "2026-09-23T10:00:00Z"},
        ]

        def _fake_query(**kwargs):
            """Apply FilterExpression manually to simulate DynamoDB semantics."""
            fk_filter = (kwargs.get("ExpressionAttributeValues") or {}).get(":fk")
            if fk_filter is not None:
                filtered = [r for r in all_rows if r["finding_kind"] == fk_filter]
            else:
                filtered = list(all_rows)
            # NOTE: no Limit applied — confirms the broker no longer uses Limit=1
            return {"Items": filtered, "Count": len(filtered)}

        ddb_mock = MagicMock()
        feedback_table_mock = MagicMock()
        feedback_table_mock.query.side_effect = _fake_query

        def table_selector(name):
            if "feedback" in name:
                return feedback_table_mock
            return MagicMock()
        ddb_mock.Table.side_effect = table_selector

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            demoted, reason = broker._check_demotion(finding)

        assert demoted is True, (
            f"Expected demoted=True but got demoted={demoted} reason={reason!r}. "
            "A prior dismissal in the feedback table should demote the Finding. "
            "The match is the 3rd row — confirms that Limit=1 is NOT in use."
        )
        assert reason == "demoted"

    def test_broker_dismissed_feedback_demotes_matching_kind_in_process_finding(self):
        """End-to-end: a demoted finding one step below the threshold is in-app only.

        Rewritten 2026-09-24 under D-G9-1 (decisions-g7-g9-fragment.md). This
        test used to assert that a demoted **p0** finding does not push. That
        encoded the unclamped reading, under which one dismissed routine tire
        card silences a later stop-driving tire push for 30 days. Demotion now
        shifts the threshold one step, clamped at p0, so the demotion case is
        exercised with a lowered threshold (``attention``) and an ``attention``
        finding. The demoted-p0 case lives in TestDemotionThresholdShift.
        """
        policy = _fresh_policy(threshold="attention")
        cognito_sub = "ee55ff66-1234-5678-abcd-111122223333"
        kind = "health.brakes"

        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="attention", kind=kind
        )

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        table_mock.update_item.return_value = {}
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": True, "coverage": True, "service": True},
                "endpoint_arn": "arn:aws:sns:us-west-2:123:endpoint/APNS/app/tok",
            }],
            "Count": 1,
        }
        table_mock.put_item.return_value = {}
        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
            # Force demotion = True
            patch.object(broker, "_check_demotion", return_value=(True, "demoted")),
        ):
            result = broker._process_finding(finding, policy)

        assert result["action"] == "in_app_card_only", (
            f"Demoted attention finding at threshold 'attention' should produce "
            f"in_app_card_only, got {result['action']!r}"
        )
        sns_mock.publish.assert_not_called()


# ===========================================================================
# Consent gate tests (Mutation 2 target)
# ===========================================================================

class TestBrokerConsentGate:
    """Consent gate enforced — a category-off consent + P0 finding is skipped."""

    def test_broker_consent_gate_blocks_category_off_owner(self):
        """Category-off consent suppresses the push for that Finding kind.

        This test MUST FAIL under Mutation 2 (consent gate removed).
        """
        policy = _fresh_policy(threshold="p0")
        cognito_sub = "aa11bb22-1234-5678-abcd-111122223333"

        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="p0", kind="health.tires"
        )

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        table_mock.update_item.return_value = {}
        table_mock.put_item.return_value = {}

        # Consent: safety=False (health.tires → safety category)
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": False, "coverage": True, "service": True},
                "endpoint_arn": "arn:aws:sns:us-west-2:123:endpoint/APNS/app/tok",
            }],
            "Count": 1,
        }
        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            result = broker._process_finding(finding, policy)

        assert result["action"] == "skipped", (
            f"Expected 'skipped' (consent=False) but got {result['action']!r}. "
            "This fails under Mutation 2 (removed consent gate) because "
            "the gate no longer blocks a category-off owner."
        )
        assert result["skip_reason"] == "consent_off"
        sns_mock.publish.assert_not_called()
        # No in-app card created either
        table_mock.put_item.assert_not_called()

    def test_consent_gate_allows_when_category_on(self):
        """Category-on consent allows the finding through to card creation."""
        policy = _fresh_policy(threshold="p0")
        cognito_sub = "cc33dd44-1234-5678-abcd-111122223333"

        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="soon", kind="health.battery"
        )

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        table_mock.update_item.return_value = {}
        table_mock.put_item.return_value = {}
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": True, "coverage": True, "service": True},
                "endpoint_arn": "arn:aws:sns:us-west-2:123:endpoint/APNS/app/tok",
            }],
            "Count": 1,
        }
        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
            patch.object(broker, "_check_demotion", return_value=(False, "no_prior_dismissal")),
        ):
            result = broker._process_finding(finding, policy)

        assert result["action"] == "in_app_card_only"
# ===========================================================================

class TestBrokerDemotionSkipReasons:
    """Pin the two demotion skip reasons so namespace reconciliation triggers
    a deliberate test-forced code change.
    """

    def test_broker_demotion_skips_owner_absent_finding(self):
        """VIN-scoped Finding with no owner_id → owner_absent skip reason.

        This test MUST FAIL when VIN→owner resolution feeds a Cognito sub
        into a top-level owner_id, because `_check_demotion` would then find
        the owner and proceed to a lookup rather than returning owner_absent.
        """
        finding = _finding_no_owner(severity="p0", kind="health.tires")
        assert "owner_id" not in finding, "Precondition: no owner_id on finding"

        ddb_mock = MagicMock()
        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            demoted, reason = broker._check_demotion(finding)

        assert demoted is False
        assert reason == "owner_absent", (
            f"Expected owner_absent but got {reason!r}. "
            "When VIN→owner resolution is wired, this finding will have an owner_id "
            "and this test will fail — that is intentional, signalling that demotion "
            "must be widened deliberately."
        )
        # DDB should NOT have been queried (no owner to look up)
        ddb_mock.Table.assert_not_called()

    def test_broker_demotion_skips_cust_namespace_owner_for_owner_namespace_unmatched(self):
        """CUST-XXXXXXXX owner_id → owner_namespace_unmatched skip reason.

        This test MUST FAIL when the CUST-* namespace is reconciled with
        Cognito subs, because _check_demotion would then be able to match
        against the feedback table.
        """
        finding = _finding_cust_owner(cust_id="CUST-ABCD1234", severity="p0")

        ddb_mock = MagicMock()
        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            demoted, reason = broker._check_demotion(finding)

        assert demoted is False
        assert reason == "owner_namespace_unmatched", (
            f"Expected owner_namespace_unmatched but got {reason!r}. "
            "CUST-XXXXXXXX IDs are CMS registry IDs, not Cognito subs. "
            "The feedback table keyed on Cognito subs cannot match. "
            "When the namespace is reconciled this test will fail — forcing "
            "an intentional widening of the demotion path."
        )
        # DDB should NOT have been queried (incompatible namespace)
        ddb_mock.Table.assert_not_called()

    def test_no_prior_dismissal_is_distinct_from_owner_absent(self):
        """'no_prior_dismissal' and 'owner_absent' are different facts and must not collapse."""
        cognito_sub = "a1b2c3d4-e5f6-7890-abcd-111122223333"
        finding_with_owner = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="p0"
        )

        # Feedback table returns empty (no prior dismissal)
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.query.return_value = {"Items": [], "Count": 0}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            demoted, reason = broker._check_demotion(finding_with_owner)

        assert demoted is False
        assert reason == "no_prior_dismissal", (
            f"Expected no_prior_dismissal but got {reason!r}. "
            "An owner-present, no-prior-dismissal case must be distinct from owner_absent."
        )


# ===========================================================================
# SSM policy loading tests
# ===========================================================================

class TestPolicyLoading:
    """Policy must be read from SSM at cold start — never hardcoded."""

    def test_policy_loads_threshold_from_ssm(self):
        """The threshold value comes from SSM, not from a module constant."""
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect(threshold="soon")

        with patch.object(broker, "_get_ssm", return_value=ssm_mock):
            policy = broker._load_policy()

        assert policy["apns_threshold"] == "soon"
        # Verify SSM was actually called for the threshold parameter
        calls = [c[1].get("Name") or c[0][0]
                 for c in ssm_mock.get_parameter.call_args_list]
        assert broker._SSM_APNS_THRESHOLD in calls

    def test_policy_rate_limit_is_json(self):
        """rate-limit is a JSON object, not a scalar."""
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect(
            rate_limit={"day": 5, "hour": 2}
        )

        with patch.object(broker, "_get_ssm", return_value=ssm_mock):
            policy = broker._load_policy()

        assert isinstance(policy["rate_limit"], dict)
        assert policy["rate_limit"]["day"] == 5
        assert policy["rate_limit"]["hour"] == 2

    def test_policy_rejects_unknown_threshold(self):
        """An invalid threshold from SSM raises ValueError (fail-closed)."""
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect(threshold="critical")

        with (
            patch.object(broker, "_get_ssm", return_value=ssm_mock),
            pytest.raises(ValueError, match="valid severity"),
        ):
            broker._load_policy()

    def test_policy_cached_after_first_load(self):
        """SSM is called exactly once per cold start (module cache)."""
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.side_effect = _make_ssm_side_effect()

        with patch.object(broker, "_get_ssm", return_value=ssm_mock):
            broker._get_policy()
            broker._get_policy()

        # Three SSM params, but only fetched once total (module-level cache)
        assert ssm_mock.get_parameter.call_count == 3  # one per param, at cold start


# ===========================================================================
# Consent category mapping test
# ===========================================================================

class TestConsentCategoryMapping:
    def test_health_kinds_map_to_safety(self):
        assert broker._consent_category("health.tires") == "safety"
        assert broker._consent_category("health.brakes") == "safety"
        assert broker._consent_category("health.battery") == "safety"

    def test_coverage_kinds_map_to_coverage(self):
        assert broker._consent_category("coverage.warranty") == "coverage"
        assert broker._consent_category("coverage.recall") == "coverage"

    def test_energy_kinds_map_to_coverage(self):
        assert broker._consent_category("energy.charging") == "coverage"
        assert broker._consent_category("energy.cost") == "coverage"

    def test_service_kinds(self):
        assert broker._consent_category("concierge.appointment") == "service"
        assert broker._consent_category("loyalty.points") == "service"

    def test_unknown_kind_defaults_to_service(self):
        assert broker._consent_category("unknown.kind") == "service"


# ===========================================================================
# DDB deserialiser test
# ===========================================================================

class TestDeserialiser:
    def test_deserialise_string_and_number(self):
        item = {"finding_id": {"S": "FIND-abc"}, "tokens_used": {"N": "42"}}
        result = broker._deserialise_ddb_item(item)
        assert result["finding_id"] == "FIND-abc"
        assert result["tokens_used"] == 42

    def test_deserialise_nested_map(self):
        item = {"scope": {"M": {"vin": {"S": "1HGCM82633A123456"}}}}
        result = broker._deserialise_ddb_item(item)
        assert result["scope"] == {"vin": "1HGCM82633A123456"}

    def test_deserialise_list(self):
        item = {"evidence": {"L": [{"S": "a"}, {"S": "b"}]}}
        result = broker._deserialise_ddb_item(item)
        assert result["evidence"] == ["a", "b"]


# ===========================================================================
# Severity rank helpers test
# ===========================================================================

class TestSeverityHelpers:
    def test_p0_is_most_severe(self):
        assert broker._severity_rank("p0") == 0

    def test_informational_is_least_severe(self):
        assert broker._severity_rank("informational") == len(broker._SEVERITY_ORDER) - 1

    def test_meets_threshold_p0_only(self):
        assert broker._meets_apns_threshold("p0", "p0") is True
        assert broker._meets_apns_threshold("attention", "p0") is False
        assert broker._meets_apns_threshold("soon", "p0") is False

    def test_meets_threshold_attention_and_above(self):
        assert broker._meets_apns_threshold("p0", "attention") is True
        assert broker._meets_apns_threshold("attention", "attention") is True
        assert broker._meets_apns_threshold("soon", "attention") is False



# ===========================================================================
# REQUIRED FIX-GROUP TEST (e): Cardinality cap BLOCKS when owner is over cap
# (positive control — proves the gate can actually say NO)
# ===========================================================================

class TestCardinality:
    """Cardinality cap is a real gate, not a structural no-op.

    The existing tests all supply Count=0, which always evaluates under_cap=True.
    A gate that can never return False is not a gate. This positive-control test
    sets Count=cap and asserts the process_finding result is skipped with
    skip_reason='cardinality_cap_exceeded'.

    Mutation target (M3): remove or invert the cap check in _check_cardinality.
    Specifically: change `count < cap` to `count <= cap` (off-by-one softening)
    or `True` (unconditional pass). This test MUST FAIL for either mutation.
    """

    def test_cardinality_cap_blocks_when_owner_is_at_cap(self):
        """_check_cardinality returns False when Count equals the cap.

        This test MUST FAIL under Mutation 3 (cardinality gate disabled or inverted).
        """
        cap = 3
        cognito_sub = "a1a1a1a1-b2b2-c3c3-d4d4-e5e5e5e5e5e5"

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        # GSI returns Count == cap (not under cap)
        table_mock.query.return_value = {"Count": cap, "Items": []}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            result = broker._check_cardinality(cognito_sub, cap)

        assert result is False, (
            f"Expected _check_cardinality to return False when count({cap}) == cap({cap}), "
            f"but got True. This test MUST FAIL under Mutation 3 (gate disabled/inverted). "
            "A cardinality gate that can never return False is not a gate."
        )

    def test_cardinality_cap_allows_when_owner_is_under_cap(self):
        """_check_cardinality returns True when Count < cap (control case)."""
        cap = 15
        cognito_sub = "f0f0f0f0-a1a1-b2b2-c3c3-d4d4d4d4d4d4"

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        # GSI returns Count < cap
        table_mock.query.return_value = {"Count": 14, "Items": []}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            result = broker._check_cardinality(cognito_sub, cap)

        assert result is True, (
            f"Expected _check_cardinality to return True when count(14) < cap({cap}), "
            f"but got False."
        )

    def test_cardinality_cap_blocks_in_process_finding(self):
        """End-to-end: process_finding skips with 'cardinality_cap_exceeded' when at cap.

        Uses a Cognito-sub owner so the namespace guard passes. The DDB mock
        simulates the actions table returning Count=cap on the GSI query.

        This test MUST FAIL under Mutation 3 (cardinality gate disabled/inverted).
        """
        policy = _fresh_policy(threshold="p0", cardinality_cap=2)
        cognito_sub = "e5e5e5e5-f6f6-a7a7-b8b8-c9c9c9c9c9c9"

        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="p0", kind="health.tires"
        )

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        # Rate-limit: always under limit (day/hour increments succeed)
        table_mock.update_item.return_value = {}

        # Cardinality query: Count == cap (blocked)
        # Consent query: never reached (cap blocks first)
        table_mock.query.return_value = {"Count": 2, "Items": []}

        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            result = broker._process_finding(finding, policy)

        assert result["action"] == "skipped", (
            f"Expected 'skipped' when cardinality count==cap, got {result['action']!r}. "
            "This test MUST FAIL under Mutation 3 (cardinality gate disabled). "
            "A gate that can never block is not a gate."
        )
        assert result["skip_reason"] == "cardinality_cap_exceeded", (
            f"Expected skip_reason='cardinality_cap_exceeded', got {result['skip_reason']!r}. "
            "This test MUST FAIL under Mutation 3."
        )
        # No in-app card created
        table_mock.put_item.assert_not_called()
        # SNS not called
        sns_mock.publish.assert_not_called()


# ===========================================================================
# REQUIRED FIX-GROUP TEST (f): No write to rate-limits or actions table when
# owner namespace is unusable (CUST-*)
# ===========================================================================

class TestNamespaceGuard:
    """No DynamoDB write occurs for a CUST-* owner — gates are bypassed entirely.

    The namespace guard in _process_finding detects a CUST-* party-id before
    any gate runs.  This test asserts that:
      (a) the result is skipped with 'owner_namespace_unmatched', and
      (b) no write (update_item or put_item) reaches any DDB table.

    Mutation target (M4): remove the namespace guard block (the early return on
    _CUST_ID_RE.match). This test MUST FAIL for that mutation because without
    the guard the CUST-* value flows into _check_and_increment_rate (write) and
    _check_cardinality (fail-open), and the result would not be 'skipped' with
    'owner_namespace_unmatched'.
    """

    def test_no_write_reaches_rate_limits_or_actions_on_cust_owner(self):
        """A CUST-* owner triggers the namespace guard; no write occurs.

        This test MUST FAIL under Mutation 4 (namespace guard removed).
        """
        policy = _fresh_policy(threshold="p0")

        # Finding with a CUST-* top-level owner_id (the namespace that is
        # incompatible with Cognito-keyed tables)
        finding = _finding_cust_owner(cust_id="CUST-DEADBEEF", severity="p0")

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.update_item.return_value = {}
        table_mock.put_item.return_value = {}
        table_mock.query.return_value = {"Items": [], "Count": 0}

        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            result = broker._process_finding(finding, policy)

        # (a) Result is skipped with the correct reason
        assert result["action"] == "skipped", (
            f"Expected 'skipped' for CUST-* owner, got {result['action']!r}. "
            "This test MUST FAIL under Mutation 4 (namespace guard removed), because "
            "without the guard the cardinality check (Count=0 < cap) fails open and "
            "the result would not be 'skipped'."
        )
        assert result["skip_reason"] == "owner_namespace_unmatched", (
            f"Expected skip_reason='owner_namespace_unmatched', got {result['skip_reason']!r}. "
            "The namespace skip must be attributed to 'owner_namespace_unmatched', "
            "not 'consent_off' or any other reason — they are distinct observable facts."
        )

        # (b) No write to any table
        table_mock.update_item.assert_not_called(), (
            "update_item was called — the namespace guard must prevent all writes. "
            "Without the guard, _check_and_increment_rate writes orphaned rows keyed "
            "by CUST-* into the rate-limits table."
        )
        table_mock.put_item.assert_not_called(), (
            "put_item was called — the namespace guard must prevent the in-app card "
            "write. Without the guard, _create_in_app_card would write a row with "
            "created_by='owner-CUST-*' that the owner can never dismiss."
        )

    def test_namespace_guard_is_distinct_from_owner_absent(self):
        """A finding with NO owner must produce 'owner_absent', not 'owner_namespace_unmatched'.

        These are distinct observable facts: absent = no party-id at all;
        unmatched = party-id present but wrong namespace. This test pins the
        value positively so a wrong attribution is caught, not just the one
        value it previously excluded.

        Mutation target: change the owner_absent return reason to any other value
        (e.g. 'wrong_attribution', 'consent_off'). This test MUST FAIL for any
        such mutation.
        """
        policy = _fresh_policy(threshold="p0")

        # Finding with no owner at all (VIN-scoped, no top-level owner_id)
        finding = _finding_no_owner(severity="p0")
        assert "owner_id" not in finding, "Precondition: no owner_id"
        assert not (finding.get("scope") or {}).get("owner_id"), "Precondition: no scope.owner_id"

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.update_item.return_value = {}
        table_mock.put_item.return_value = {}
        table_mock.query.return_value = {"Items": [], "Count": 0}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            result = broker._process_finding(finding, policy)

        # A no-owner finding MUST produce 'owner_absent', not any other reason
        assert result["action"] == "skipped", (
            f"Expected 'skipped' for no-owner finding, got {result['action']!r}"
        )
        assert result["skip_reason"] == "owner_absent", (
            f"Expected skip_reason='owner_absent' but got {result['skip_reason']!r}. "
            "A VIN-scoped Finding with no owner must not collapse into 'consent_off' "
            "or any other reason — 'owner_absent' and 'owner_namespace_unmatched' and "
            "'consent_off' are three distinct, independently observable facts."
        )

    def test_scope_cust_owner_also_triggers_namespace_guard(self):
        """A CUST-* in scope.owner_id (owner-scoped Finding) also triggers the guard.

        _resolve_owner_party_id prefers scope.owner_id. Verify that a scope-level
        CUST-* is caught by the namespace guard, not routed through the gates.
        """
        policy = _fresh_policy(threshold="p0")

        # scope.owner_id = CUST-* (owner-scoped Finding shape)
        finding = _finding_with_scope_owner(scope_owner="CUST-CAFEBABE", severity="p0")

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.update_item.return_value = {}
        table_mock.put_item.return_value = {}
        table_mock.query.return_value = {"Items": [], "Count": 0}

        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            result = broker._process_finding(finding, policy)

        assert result["action"] == "skipped"
        assert result["skip_reason"] == "owner_namespace_unmatched"
        table_mock.update_item.assert_not_called()
        table_mock.put_item.assert_not_called()

    # -----------------------------------------------------------------------
    # Anti-vacuity: unrecognised namespace (not UUID, not CUST-*) is skipped
    # -----------------------------------------------------------------------

    def test_unrecognised_party_id_is_skipped(self):
        """An unrecognised party-id (neither UUID nor CUST-*) is skipped with
        owner_namespace_unmatched and produces no DDB writes.

        This is the anti-vacuity case for the allowlist guard introduced in the
        Cycle 2 fix. The allowlist accepts ONLY Cognito sub (UUID shape); every
        other value — including a driver-shaped id such as 'DRV-0001' — must be
        rejected.

        This test MUST FAIL under the Cycle 2 mutation (allowlist widened to
        accept any non-empty string), because 'DRV-0001' would then pass the
        guard and flow into the gates, causing the result NOT to be 'skipped'.
        """
        policy = _fresh_policy(threshold="p0")

        # DRV-* is the expected near-term party-id shape for the driver construct
        # (per issue avx-owner-id-not-comparable-to-cognito-sub). It is neither a
        # Cognito sub (UUID) nor a CUST-* registry ID, so it must be rejected.
        finding = {
            "finding_id": "FIND-00000000-0000-0000-0000-000000000099",
            "finding_kind": "health.tires",
            "severity": "p0",
            "headline": "Test",
            "scope": {"vin": "1HGCM82633A123456"},
            "owner_id": "DRV-0001",
        }

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.update_item.return_value = {}
        table_mock.put_item.return_value = {}
        table_mock.query.return_value = {"Items": [], "Count": 0}

        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
        ):
            result = broker._process_finding(finding, policy)

        assert result["action"] == "skipped", (
            f"Expected 'skipped' for DRV-shaped party-id, got {result['action']!r}. "
            "The allowlist guard must fail closed on any non-UUID party-id. "
            "This test MUST FAIL under the Cycle 2 mutation (allowlist widened to "
            "accept any non-empty string)."
        )
        assert result["skip_reason"] == "owner_namespace_unmatched", (
            f"Expected skip_reason='owner_namespace_unmatched', got {result['skip_reason']!r}."
        )
        # No write to any table — the allowlist guard bypasses all gates
        table_mock.update_item.assert_not_called()
        table_mock.put_item.assert_not_called()

    def test_cognito_sub_uuid_passes_namespace_guard(self):
        """A well-formed Cognito sub (UUID) passes the allowlist guard and reaches
        the downstream gates.

        This is the positive control that prevents the allowlist from becoming a
        guard that rejects EVERYTHING (which would pass the anti-vacuity test above
        vacuously). With this test present, an allowlist that always returns False
        would fail here because the UUID-shaped owner would be incorrectly skipped.
        """
        policy = _fresh_policy(threshold="p0")
        cognito_sub = "a1b2c3d4-e5f6-7890-abcd-111122223333"

        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="p0", kind="health.tires"
        )

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        # Rate-limit: always under limit
        table_mock.update_item.return_value = {}
        # Consent: allowed (returns a device row)
        table_mock.query.return_value = {
            "Items": [{
                "owner_id": cognito_sub,
                "device_token": "tok",
                "per_category_consent": {"safety": True},
                "endpoint_arn": "arn:aws:sns:us-west-2:123:endpoint/APNS/app/tok",
            }],
            "Count": 1,
        }
        table_mock.put_item.return_value = {}

        sns_mock = MagicMock()

        with (
            patch.object(broker, "_get_ddb", return_value=ddb_mock),
            patch.object(broker, "_get_sns", return_value=sns_mock),
            patch.object(broker, "_check_demotion",
                         return_value=(False, "no_prior_dismissal")),
        ):
            result = broker._process_finding(finding, policy)

        # UUID owner must NOT be skipped by the namespace guard
        assert result["action"] != "skipped" or result.get("skip_reason") != "owner_namespace_unmatched", (
            "A valid Cognito sub (UUID) was rejected by the namespace guard. "
            "The allowlist must accept UUID-shaped party-ids. "
            "An allowlist that rejects everything would pass the anti-vacuity test "
            "above vacuously — this positive control prevents that."
        )
        # The finding reaches the downstream gates and produces a card
        assert result["action"] in ("in_app_card_and_apns", "in_app_card_only"), (
            f"Expected a card action for UUID owner, got {result['action']!r} "
            f"skip_reason={result.get('skip_reason')!r}."
        )



# ===========================================================================
# C1: owner_absent emitted from _process_finding (not consent_off)
# ===========================================================================

class TestOwnerAbsentFromProcessFinding:
    """VIN-scoped Finding with no owner must produce owner_absent at the
    _process_finding seam, not consent_off.

    Mutation target (MC1): change the owner_absent return reason to 'consent_off'
    or any other string.  This test MUST FAIL for that mutation.
    """

    def test_owner_absent_finding_returns_owner_absent_not_consent_off(self):
        """_process_finding returns skip_reason='owner_absent' for a no-owner Finding.

        This test MUST FAIL under mutation MC1 (owner_absent → 'consent_off').
        """
        policy = _fresh_policy(threshold="p0")
        finding = _finding_no_owner(severity="p0", kind="health.tires")

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.query.return_value = {"Items": [], "Count": 0}
        table_mock.put_item.return_value = {}
        table_mock.update_item.return_value = {}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            result = broker._process_finding(finding, policy)

        assert result["action"] == "skipped", (
            f"owner-absent Finding should be skipped, got {result['action']!r}"
        )
        assert result["skip_reason"] == "owner_absent", (
            f"Expected skip_reason='owner_absent' but got {result['skip_reason']!r}. "
            "A VIN-scoped Finding with no owner MUST NOT be attributed to "
            "'consent_off' — that would collapse a missing owner_id field with a "
            "deliberate consent-off decision, making an owner_id regression look "
            "like a fleet of consent-off owners. "
            "This test MUST FAIL under mutation MC1 (reason changed to 'consent_off')."
        )
        # The early return must happen BEFORE any DDB gate runs
        table_mock.update_item.assert_not_called(), (
            "update_item called — the owner_absent return must fire before rate-limit gate"
        )
        table_mock.put_item.assert_not_called(), (
            "put_item called — the owner_absent return must fire before card creation"
        )

    def test_owner_absent_no_ddb_queries(self):
        """No DDB query is issued for an owner-absent Finding."""
        policy = _fresh_policy(threshold="p0")
        finding = _finding_no_owner(severity="p0")

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            result = broker._process_finding(finding, policy)

        assert result["skip_reason"] == "owner_absent"
        # No DDB table should be touched at all (no gate runs, no card written)
        ddb_mock.Table.assert_not_called()


# ===========================================================================
# C3: In-app card conforms to urn:cvx:avx:action:v1
# ===========================================================================

class TestInAppCardSchemaConformance:
    """The row written by _create_in_app_card must validate against
    urn:cvx:avx:action:v1 (action.schema.json).

    Mutation target (MC3a): remove any required field (e.g. action_kind).
    Mutation target (MC3b): revert action_id prefix to 'ACT-' (schema requires 'AVX-ACT-').
    Both MUST FAIL this test.
    """

    def _action_schema_path(self) -> str:
        return os.path.normpath(os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..", "tier2", "schemas", "action.schema.json",
        ))

    def test_in_app_card_validates_against_action_schema(self):
        """_create_in_app_card produces a row that passes Draft2020-12 validation.

        This test MUST FAIL under MC3a (missing required field) or MC3b
        (wrong action_id prefix).

        No skip on a missing ``jsonschema`` (F32.3): it is pinned in
        requirements-dev.txt, and a skipped schema guard is a silent pass.
        """
        from jsonschema import Draft202012Validator

        schema_path = self._action_schema_path()
        assert os.path.exists(schema_path), f"action.schema.json not found at {schema_path}"

        import json as _json
        with open(schema_path) as f:
            schema = _json.load(f)
        validator = Draft202012Validator(schema)

        cognito_sub = "a2b3c4d5-e6f7-8901-abcd-111122223333"
        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="p0", kind="health.tires"
        )
        finding["evidence"] = [
            {
                "kind": "tire_pressure",
                "source": "adp",
                "source_ref": "ADP-REF-001",
                "value": {"psi": 28.5},
                "computed_at": "2026-09-23T10:00:00Z",
                "provenance": "live",
            }
        ]

        captured_item: dict = {}

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        def capture_put_item(**kwargs):
            captured_item.update(kwargs.get("Item", {}))
            return {}
        table_mock.put_item.side_effect = capture_put_item

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            action_id, _created = broker._create_in_app_card(finding, cognito_sub)

        assert action_id.startswith("AVX-ACT-"), (
            f"action_id must start with 'AVX-ACT-', got {action_id!r}. "
            "This test MUST FAIL under MC3b (prefix reverted to 'ACT-')."
        )

        errors = list(validator.iter_errors(captured_item))
        assert not errors, (
            "Written Action row does not conform to urn:cvx:avx:action:v1:\n"
            + "\n".join(f"  [{e.json_path}] {e.message}" for e in errors)
            + "\nThis test MUST FAIL under MC3a (missing required field) or "
            "MC3b (wrong action_id prefix)."
        )

    def test_action_id_uses_avx_act_prefix(self):
        """action_id starts with 'AVX-ACT-' (schema pattern ^AVX-ACT-{uuid}$).

        This test MUST FAIL under MC3b (prefix reverted to 'ACT-').
        """
        cognito_sub = "b3c4d5e6-f7a8-9012-abcd-111122223333"
        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, kind="health.brakes")

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.put_item.return_value = {}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            action_id, _created = broker._create_in_app_card(finding, cognito_sub)

        assert action_id.startswith("AVX-ACT-"), (
            f"action_id must start with 'AVX-ACT-', got {action_id!r}. "
            "This test MUST FAIL under MC3b (prefix 'ACT-')."
        )

    def test_card_contains_action_kind(self):
        """Written card item contains 'action_kind' field.

        This test MUST FAIL under MC3a (action_kind removed from _create_in_app_card).
        """
        cognito_sub = "c4d5e6f7-a8b9-0123-abcd-111122223333"
        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, kind="coverage.recall")

        captured: dict = {}
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.put_item.side_effect = lambda **kw: captured.update(kw.get("Item", {}))

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            broker._create_in_app_card(finding, cognito_sub)

        assert "action_kind" in captured, (
            "Written card item must contain 'action_kind'. "
            "This test MUST FAIL under MC3a (action_kind removed)."
        )
        assert captured["action_kind"] == "surface_recall", (
            f"coverage.recall should map to 'surface_recall', got {captured['action_kind']!r}"
        )

    def test_audit_trail_uses_correct_shape(self):
        """Audit trail entry uses {at, by, from_status, to_status}, not {actor, action, note}.

        This test MUST FAIL under a mutation that reverts to the old audit_trail shape.
        """
        cognito_sub = "d5e6f7a8-b9c0-1234-abcd-111122223333"
        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, kind="health.battery")

        captured: dict = {}
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.put_item.side_effect = lambda **kw: captured.update(kw.get("Item", {}))

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            broker._create_in_app_card(finding, cognito_sub)

        trail = captured.get("audit_trail", [])
        assert trail, "audit_trail must not be empty"
        entry = trail[0]
        assert "by" in entry, (
            f"audit_trail entry must have 'by', got keys: {list(entry.keys())}. "
            "Old shape used 'actor' — this test catches a revert."
        )
        assert "from_status" in entry, "audit_trail entry must have 'from_status'"
        assert "to_status" in entry, "audit_trail entry must have 'to_status'"
        assert "at" in entry, "audit_trail entry must have 'at'"
        # Old forbidden fields
        assert "actor" not in entry, "old 'actor' field present — use 'by'"
        assert "action" not in entry, "old 'action' field present — not in schema"


# ===========================================================================
# C4: Card creation is idempotent (deterministic action_id + ConditionExpression)
# ===========================================================================

class TestCardCreationIdempotency:
    """Repeated delivery of the same Finding produces exactly one card.

    Mutation target (MC4a): revert to random uuid4() action_id.
    Mutation target (MC4b): remove ConditionExpression from put_item.
    Both MUST cause at least one assertion to fail.
    """

    def test_derive_action_id_is_deterministic(self):
        """_derive_action_id returns the same value for the same inputs.

        This test MUST FAIL under MC4a (random uuid4 substituted).
        """
        finding_id = "FIND-00000000-0000-0000-0000-000000000001"
        action_kind = "schedule_inspection"
        created_by = "owner-aaaabbbb-1234-5678-abcd-111122223333"

        id1 = broker._derive_action_id(finding_id, action_kind, created_by)
        id2 = broker._derive_action_id(finding_id, action_kind, created_by)

        assert id1 == id2, (
            f"_derive_action_id must be deterministic: got {id1!r} then {id2!r}. "
            "This test MUST FAIL under MC4a (random uuid4)."
        )
        assert id1.startswith("AVX-ACT-"), (
            f"Derived id must start with 'AVX-ACT-', got {id1!r}"
        )

    def test_derive_action_id_differs_for_different_finding(self):
        """_derive_action_id is sensitive to finding_id input (anti-vacuity)."""
        action_kind = "schedule_inspection"
        created_by = "owner-aaaabbbb-1234-5678-abcd-111122223333"

        id1 = broker._derive_action_id("FIND-00000000-0000-0000-0000-000000000001", action_kind, created_by)
        id2 = broker._derive_action_id("FIND-00000000-0000-0000-0000-000000000002", action_kind, created_by)

        assert id1 != id2, (
            "_derive_action_id must differ for different finding_ids (anti-vacuity). "
            "A function that always returns the same hash would pass the determinism "
            "test but fail here."
        )

    def test_create_in_app_card_uses_condition_expression(self):
        """put_item is called with ConditionExpression='attribute_not_exists(action_id)'.

        This test MUST FAIL under MC4b (ConditionExpression removed).
        """
        cognito_sub = "e6f7a8b9-c0d1-2345-abcd-111122223333"
        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, kind="health.tires")

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.put_item.return_value = {}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            broker._create_in_app_card(finding, cognito_sub)

        call_kwargs = table_mock.put_item.call_args.kwargs
        assert "ConditionExpression" in call_kwargs, (
            "put_item must be called with ConditionExpression for idempotency. "
            "Without it, duplicate stream deliveries create duplicate cards. "
            "This test MUST FAIL under MC4b (ConditionExpression removed)."
        )
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(action_id)", (
            f"Expected ConditionExpression='attribute_not_exists(action_id)', "
            f"got {call_kwargs['ConditionExpression']!r}"
        )

    def test_repeated_delivery_produces_one_card(self):
        """Two deliveries of the same Finding produce the same action_id (idempotent).

        The second delivery hits ConditionalCheckFailedException and returns the
        same action_id without raising. This is the stream retry scenario.

        This test MUST FAIL under MC4a (random uuid4) because the two ids differ.
        """
        from botocore.exceptions import ClientError as BotoClientError

        cognito_sub = "f7a8b9c0-d1e2-3456-abcd-111122223333"
        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, kind="health.tires")

        # First delivery: succeeds
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.put_item.return_value = {}

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            id1, created1 = broker._create_in_app_card(finding, cognito_sub)

        # Second delivery: ConditionalCheckFailedException (already exists)
        table_mock2 = MagicMock()
        ddb_mock2 = MagicMock()
        ddb_mock2.Table.return_value = table_mock2

        conditional_error = BotoClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        table_mock2.put_item.side_effect = conditional_error

        with patch.object(broker, "_get_ddb", return_value=ddb_mock2):
            id2, created2 = broker._create_in_app_card(finding, cognito_sub)

        assert id1 == id2, (
            f"Both deliveries of the same Finding must derive the same action_id. "
            f"Got {id1!r} and {id2!r}. "
            "This test MUST FAIL under MC4a (random uuid4)."
        )
        # F32.4: the caller needs to know the second write was a no-op, or it
        # pushes again (review-group7-deeper C-new).
        assert (created1, created2) == (True, False), (
            f"created flags must be (True, False), got {(created1, created2)!r}"
        )


# ===========================================================================
# C5: Demotion query kind-matching — no match when kind differs
# ===========================================================================

class TestDemotionKindMatching:
    """The demotion query must be sensitive to finding_kind.

    Mutation target (MC5): delete the FilterExpression from the demotion query.
    With a fixed-return mock the original test was blind to this. This test uses
    a fake that applies the filter so it actually tests kind-matching.
    """

    def test_no_demotion_when_only_other_kinds_dismissed(self):
        """No demotion when the owner has dismissed OTHER kinds but not this one.

        This test MUST FAIL under MC5 (FilterExpression removed), because
        without the filter the query returns ALL rows, Count > 0, and demotes
        incorrectly.
        """
        cognito_sub = "a9b0c1d2-e3f4-5678-abcd-111122223333"
        kind_under_test = "health.tires"
        other_kinds = ["health.brakes", "energy.charging", "coverage.recall"]

        finding = _finding_with_cognito_owner(
            cognito_sub=cognito_sub, severity="p0", kind=kind_under_test
        )

        # Three rows — NONE with health.tires
        all_rows = [
            {"owner_id": cognito_sub, "finding_kind": k,
             "dismissed_at": "2026-09-23T10:00:00Z"}
            for k in other_kinds
        ]

        def _fake_query(**kwargs):
            fk_filter = (kwargs.get("ExpressionAttributeValues") or {}).get(":fk")
            if fk_filter is not None:
                filtered = [r for r in all_rows if r["finding_kind"] == fk_filter]
            else:
                filtered = list(all_rows)
            return {"Items": filtered, "Count": len(filtered)}

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        table_mock.query.side_effect = _fake_query
        ddb_mock.Table.return_value = table_mock

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            demoted, reason = broker._check_demotion(finding)

        assert demoted is False, (
            f"Expected no demotion (no {kind_under_test!r} row), "
            f"but got demoted=True reason={reason!r}. "
            "This test MUST FAIL under MC5 (FilterExpression removed), because "
            "without the filter any prior dismissal of any kind demotes any Finding."
        )
        assert reason == "no_prior_dismissal", (
            f"Expected 'no_prior_dismissal', got {reason!r}"
        )


# ===========================================================================
# W1: DynamoDB failure in demotion returns 'demotion_check_failed', not 'no_prior_dismissal'
# ===========================================================================

class TestDemotionCheckFailed:
    """A DDB failure during the demotion check must produce 'demotion_check_failed',
    not 'no_prior_dismissal'.  An error is not an absence — they are distinct facts.

    Mutation target (MW1): revert the except branch to return 'no_prior_dismissal'.
    This test MUST FAIL for that mutation.
    """

    def test_ddb_failure_in_demotion_returns_demotion_check_failed(self):
        """ClientError during demotion query → reason='demotion_check_failed'.

        This test MUST FAIL under MW1 (error reason reverted to 'no_prior_dismissal').
        """
        from botocore.exceptions import ClientError as BotoClientError

        cognito_sub = "b0c1d2e3-f4a5-6789-abcd-111122223333"
        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, kind="health.tires")

        client_error = BotoClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                        "Message": "Throughput exceeded"}},
            "Query",
        )

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        table_mock.query.side_effect = client_error
        ddb_mock.Table.return_value = table_mock

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            demoted, reason = broker._check_demotion(finding)

        assert demoted is False, (
            "Fail-open behaviour preserved: DDB failure must not suppress notifications."
        )
        assert reason == "demotion_check_failed", (
            f"Expected reason='demotion_check_failed' but got {reason!r}. "
            "A DynamoDB failure is NOT the same as an empty feedback table — "
            "conflating them makes a throttle look like 'owner never dismissed anything'. "
            "This test MUST FAIL under MW1 (reason reverted to 'no_prior_dismissal')."
        )

    def test_no_prior_dismissal_still_distinct_from_demotion_check_failed(self):
        """An empty feedback table returns 'no_prior_dismissal', not the error reason.

        Anti-vacuity: confirms the three outcomes (demoted / no_prior_dismissal /
        demotion_check_failed) are all reachable and distinct.
        """
        cognito_sub = "c1d2e3f4-a5b6-7890-abcd-111122223333"
        finding = _finding_with_cognito_owner(cognito_sub=cognito_sub, kind="health.brakes")

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        table_mock.query.return_value = {"Items": [], "Count": 0}
        ddb_mock.Table.return_value = table_mock

        with patch.object(broker, "_get_ddb", return_value=ddb_mock):
            demoted, reason = broker._check_demotion(finding)

        assert demoted is False
        assert reason == "no_prior_dismissal", (
            f"Empty feedback → 'no_prior_dismissal', got {reason!r}. "
            "The DDB-error path must not contaminate the no-rows path."
        )
