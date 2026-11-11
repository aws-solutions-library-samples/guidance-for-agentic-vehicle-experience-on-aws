"""T1.2a — Broker p0 push text is the directive word for word.

Spec: .kiro/specs/2026-11-05-avx-proactive-p0-alert/spec.md § P

For a Finding with severity == "p0" whose stored "detail" text passes
dtc_response_catalog.is_p0_directive, _send_apns_push must build:
  - default: the directive text (byte-identical to finding["detail"])
  - APNS and APNS_SANDBOX (identical strings):
      aps.alert = {"title": "Critical vehicle alert", "body": <directive>}
      aps.category = "AVX_SAFETY"
      aps.sound = "default"
      custom root keys: finding_id, finding_kind, severity, open = "assistant"

Any other Finding keeps the legacy headline-based payload exactly (pinned).
Tests parse the published Message JSON — no substring checks.

Mutations run against this file (decisions.md § T1.2a):
  M1: headline as body instead of finding["detail"]         → caught
  M2: body trimmed to [:80]                                  → caught
  M3: body lowercased                                        → caught
  M4: directive payload for severity != "p0" (attention)    → caught
  M5: is_p0_directive gate removed; any p0 text used         → caught
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_avx_broker_p0_escalation.py)
# ---------------------------------------------------------------------------
_LAMBDAS_DIR = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import avx_broker as broker  # noqa: E402
from agents.supervisor.dtc_response_catalog import (  # noqa: E402
    is_p0_directive,
    p0_directive,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
_OWNER = "aaaabbbb-1234-5678-abcd-111122223333"
_ENDPOINT = "arn:aws:sns:us-west-2:123456789012:endpoint/APNS_SANDBOX/vsa/testtoken"
_FID = "FND-P217-BRAKE-001"
_KIND = "health.brakes"

# A real catalog directive (fallback template, no first name).
# p0_directive([], None) → "This is critical — pull over safely right now, ..."
_DIRECTIVE = p0_directive([], None)

# Sanity: our test directive must actually pass the gate
assert is_p0_directive(_DIRECTIVE), (
    f"test setup error: _DIRECTIVE does not pass is_p0_directive: {_DIRECTIVE!r}"
)

# A non-directive p0 text (LLM-style prose)
_LLM_P0_TEXT = (
    "Your vehicle has a critical brake system issue requiring immediate attention."
)
assert not is_p0_directive(_LLM_P0_TEXT), "test setup: _LLM_P0_TEXT must NOT pass is_p0_directive"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_broker_singletons():
    broker._ddb = None
    broker._ssm = None
    broker._sns = None
    yield
    broker._ddb = None
    broker._ssm = None
    broker._sns = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ddb_with_device(endpoint_arn: str = _ENDPOINT) -> MagicMock:
    table = MagicMock()
    table.query.return_value = {
        "Items": [{"owner_id": _OWNER, "endpoint_arn": endpoint_arn}],
        "Count": 1,
    }
    ddb = MagicMock()
    ddb.Table.return_value = table
    return ddb


def _call_send_apns_push(finding: dict) -> tuple[str, dict | None]:
    """Drive _send_apns_push with mocked DDB + SNS.

    Returns (outcome, sns_publish_kwargs) where kwargs is the keyword dict
    passed to sns.publish(), or None if publish was not called.
    """
    ddb = _make_ddb_with_device()
    sns = MagicMock()
    with (
        patch.object(broker, "_get_ddb", return_value=ddb),
        patch.object(broker, "_get_sns", return_value=sns),
    ):
        outcome = broker._send_apns_push(_OWNER, finding)
    kwargs = sns.publish.call_args[1] if sns.publish.called else None
    return outcome, kwargs


def _parse_message(kwargs: dict | None) -> dict:
    """Parse the outer Message JSON from sns.publish kwargs."""
    assert kwargs is not None, "sns.publish was not called — check device/endpoint setup"
    return json.loads(kwargs["Message"])


def _p0_directive_finding(finding_text: str | None = None, kind: str = _KIND) -> dict:
    """A p0 Finding whose 'finding' field is (by default) the real catalog directive."""
    text = _DIRECTIVE if finding_text is None else finding_text
    return {
        "finding_id": _FID,
        "finding_kind": kind,
        "severity": "p0",
        "headline": "Critical vehicle alert — stop driving",
        "detail": text,
    }


def _non_p0_finding(severity: str = "attention") -> dict:
    return {
        "finding_id": "FND-TIRE-002",
        "finding_kind": "health.tires",
        "severity": severity,
        "headline": "Tire pressure low on front-left",
        "detail": "Your front-left tire pressure is 20 PSI below the recommended level.",
    }


# ---------------------------------------------------------------------------
# P0 directive payload — core property tests
# ---------------------------------------------------------------------------

class TestP0DirectivePayload:
    """A p0 Finding with a catalog directive sends the directive as the alert body."""

    def test_outcome_is_published(self):
        outcome, _ = _call_send_apns_push(_p0_directive_finding())
        assert outcome == "published"

    def test_default_is_directive_text_not_headline(self):
        """'default' must equal finding['finding'], not the headline."""
        finding = _p0_directive_finding()
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        assert msg["default"] == finding["detail"]
        assert msg["default"] != finding["headline"], (
            "default must be the directive body, not the headline"
        )

    def test_apns_alert_is_dict(self):
        """aps.alert must be a dict {"title": ..., "body": ...}."""
        _, kwargs = _call_send_apns_push(_p0_directive_finding())
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        alert = apns["aps"]["alert"]
        assert isinstance(alert, dict), (
            f"aps.alert must be a dict for p0 directive, got {type(alert).__name__}: {alert!r}"
        )

    def test_apns_alert_title(self):
        """aps.alert.title == 'Critical vehicle alert'."""
        _, kwargs = _call_send_apns_push(_p0_directive_finding())
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert apns["aps"]["alert"]["title"] == "Critical vehicle alert"

    def test_apns_alert_body_is_directive_byte_identical(self):
        """aps.alert.body is the directive string, byte-identical to finding['finding']."""
        finding = _p0_directive_finding()
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert apns["aps"]["alert"]["body"] == finding["detail"]

    def test_apns_category_avx_safety(self):
        """aps.category == 'AVX_SAFETY'."""
        _, kwargs = _call_send_apns_push(_p0_directive_finding())
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert apns["aps"].get("category") == "AVX_SAFETY"

    def test_apns_open_is_assistant(self):
        """Custom key 'open' at the APNS payload root == 'assistant'."""
        _, kwargs = _call_send_apns_push(_p0_directive_finding())
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert apns.get("open") == "assistant"

    def test_apns_custom_metadata_keys(self):
        """finding_id, finding_kind, severity are present at the APNS payload root."""
        finding = _p0_directive_finding()
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert apns["finding_id"] == finding["finding_id"]
        assert apns["finding_kind"] == finding["finding_kind"]
        assert apns["severity"] == "p0"

    def test_apns_sandbox_identical_to_apns(self):
        """APNS_SANDBOX and APNS are the same string (byte-identical)."""
        _, kwargs = _call_send_apns_push(_p0_directive_finding())
        msg = _parse_message(kwargs)
        assert msg["APNS"] == msg["APNS_SANDBOX"], (
            "APNS and APNS_SANDBOX must be byte-identical for p0 directive Finding"
        )

    def test_apns_message_structure_json(self):
        """sns.publish is called with MessageStructure='json'."""
        ddb = _make_ddb_with_device()
        sns = MagicMock()
        with (
            patch.object(broker, "_get_ddb", return_value=ddb),
            patch.object(broker, "_get_sns", return_value=sns),
        ):
            broker._send_apns_push(_OWNER, _p0_directive_finding())
        assert sns.publish.call_args[1]["MessageStructure"] == "json"

    def test_different_directive_also_works(self):
        """A brake_emergency directive (different template) also routes correctly."""
        from agents.supervisor.dtc_response_catalog import RESPONSE_TEMPLATES
        brake_first = RESPONSE_TEMPLATES["brake_emergency"]["first_sentence"]
        brake_directive = brake_first[:1].upper() + brake_first[1:]
        assert is_p0_directive(brake_directive), "brake directive must pass is_p0_directive"
        finding = _p0_directive_finding(finding_text=brake_directive)
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert isinstance(apns["aps"]["alert"], dict)
        assert apns["aps"]["alert"]["body"] == brake_directive
        assert apns["aps"]["alert"]["title"] == "Critical vehicle alert"


# ---------------------------------------------------------------------------
# Non-directive payload — pinned exact structure
# ---------------------------------------------------------------------------

class TestNonDirectivePayloadPinned:
    """Non-p0 and p0-with-non-directive Findings keep the legacy headline payload exactly."""

    def _assert_legacy_payload(self, finding: dict) -> None:
        """Assert the published Message matches the legacy headline-based structure."""
        headline = finding.get("headline", "New vehicle health finding")
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)

        # 'default' is the headline
        assert msg["default"] == headline

        apns = json.loads(msg["APNS"])
        apns_sb = json.loads(msg["APNS_SANDBOX"])

        # aps.alert is a string (headline), not a dict
        assert isinstance(apns["aps"]["alert"], str), (
            f"legacy payload: aps.alert must be a string, got {type(apns['aps']['alert'])}"
        )
        assert apns["aps"]["alert"] == headline

        # legacy aps fields
        assert apns["aps"]["sound"] == "default"
        assert apns["aps"]["badge"] == 1

        # No directive-specific keys
        assert "category" not in apns["aps"], "legacy payload must not have aps.category"
        assert "open" not in apns, "legacy payload must not have 'open' key"

        # Custom metadata
        assert apns["finding_id"] == finding.get("finding_id")
        assert apns["finding_kind"] == finding.get("finding_kind")
        assert apns["severity"] == finding.get("severity")

        # APNS == APNS_SANDBOX
        assert msg["APNS"] == msg["APNS_SANDBOX"]

        # APNS and APNS_SANDBOX parse identically
        assert apns == apns_sb

    def test_attention_finding_keeps_legacy_payload(self):
        self._assert_legacy_payload(_non_p0_finding("attention"))

    def test_soon_finding_keeps_legacy_payload(self):
        self._assert_legacy_payload(_non_p0_finding("soon"))

    def test_routine_finding_keeps_legacy_payload(self):
        self._assert_legacy_payload(_non_p0_finding("routine"))

    def test_p0_with_non_directive_text_keeps_legacy_payload(self):
        """A p0 Finding whose 'finding' does NOT pass is_p0_directive → legacy payload."""
        finding = _p0_directive_finding(finding_text=_LLM_P0_TEXT)
        self._assert_legacy_payload(finding)

    def test_p0_with_empty_finding_keeps_legacy_payload(self):
        """A p0 Finding with no 'finding' field → legacy payload."""
        finding = {
            "finding_id": "FND-EMPTY",
            "finding_kind": _KIND,
            "severity": "p0",
            "headline": "Critical brake alert",
            # no 'detail' key
        }
        self._assert_legacy_payload(finding)


# ---------------------------------------------------------------------------
# Mutation guards
# Each test asserts the specific property that the corresponding mutation breaks.
# See decisions.md § T1.2a for the mutation log.
# ---------------------------------------------------------------------------

class TestMutationGuards:

    def test_M1_body_is_not_headline(self):
        """M1 guard: body must be finding['finding'], not headline.
        Mutation: replace directive_text with headline in the APNS body."""
        finding = _p0_directive_finding()
        assert finding["detail"] != finding["headline"], "pre-condition: directive ≠ headline"
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        body = apns["aps"]["alert"]["body"]
        assert body == finding["detail"], (
            f"M1: body should be the directive, not the headline. "
            f"Got: {body!r}. Expected: {finding['finding']!r}"
        )
        assert body != finding["headline"]

    def test_M2_body_is_not_trimmed(self):
        """M2 guard: body must not be trimmed to a shorter string.
        Mutation: set body = directive_text[:80]."""
        finding = _p0_directive_finding()
        full_directive = finding["detail"]
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        body = apns["aps"]["alert"]["body"]
        assert len(body) == len(full_directive), (
            f"M2: body length {len(body)} != directive length {len(full_directive)}: "
            f"body was trimmed"
        )
        assert body == full_directive

    def test_M3_body_is_not_re_cased(self):
        """M3 guard: body must not be lowercased or uppercased.
        Mutation: set body = directive_text.lower()."""
        finding = _p0_directive_finding()
        directive = finding["detail"]
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        body = apns["aps"]["alert"]["body"]
        assert body == directive, (
            f"M3: body was re-cased. Got: {body!r}, expected: {directive!r}"
        )
        # The catalog directive starts with a capital letter; lowercase would differ
        assert body[0].isupper(), "M3: first character of directive must be uppercase"

    def test_M4_non_p0_severity_with_directive_text_uses_legacy(self):
        """M4 guard: directive payload must ONLY fire for severity == 'p0'.
        Mutation: remove the severity == _P0 check."""
        # attention Finding that happens to carry directive text
        finding = {
            "finding_id": "FND-ATT-DIRTEXT",
            "finding_kind": _KIND,
            "severity": "attention",  # NOT p0
            "headline": "Brakes: monitor",
            "detail": _DIRECTIVE,    # passes is_p0_directive
        }
        assert is_p0_directive(finding["detail"]), "pre-condition: finding passes is_p0_directive"
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        # Must use legacy payload (aps.alert is a string, no category)
        assert isinstance(apns["aps"]["alert"], str), (
            "M4: attention severity must use legacy payload even with directive text"
        )
        assert "category" not in apns["aps"], (
            "M4: aps.category must not be set for non-p0 Finding"
        )
        assert "open" not in apns, "M4: 'open' must not be set for non-p0 Finding"

    def test_M5_is_p0_directive_gate_prevents_llm_text_push(self):
        """M5 guard: is_p0_directive gate must prevent LLM prose from becoming directive body.
        Mutation: remove the is_p0_directive() check (any p0 text treated as directive)."""
        finding = _p0_directive_finding(finding_text=_LLM_P0_TEXT)
        # severity is p0, but finding text is NOT a catalog directive
        assert not is_p0_directive(finding["detail"])
        _, kwargs = _call_send_apns_push(finding)
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        # Must use legacy payload
        assert isinstance(apns["aps"]["alert"], str), (
            "M5: p0 with non-directive 'finding' must use legacy payload"
        )
        assert "category" not in apns["aps"]
        assert "open" not in apns

    def test_category_present_for_directive(self):
        """Negative guard for category removal mutation: aps.category must be present."""
        _, kwargs = _call_send_apns_push(_p0_directive_finding())
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert "category" in apns["aps"], (
            "aps.category must be present for p0 directive Finding"
        )
        assert apns["aps"]["category"] == "AVX_SAFETY"

    def test_open_present_for_directive(self):
        """Negative guard for open key removal: 'open' must be present at APNS root."""
        _, kwargs = _call_send_apns_push(_p0_directive_finding())
        msg = _parse_message(kwargs)
        apns = json.loads(msg["APNS"])
        assert "open" in apns, "custom key 'open' must be present for p0 directive Finding"
        assert apns["open"] == "assistant"


# ---------------------------------------------------------------------------
# The stored shape: the broker reads what the findings table actually holds
# ---------------------------------------------------------------------------

def test_reads_detail_from_a_stream_image_shaped_like_the_stored_finding():
    """The stored AVX Finding has ``detail`` and no ``finding`` (checked against
    vsa-staging-avx-findings on 2026-11-05). Deserialize a NewImage of that shape the way
    the handler does and push it: the body must be the directive. A broker reading any
    other attribute would fall back to the headline here."""
    new_image = {
        "finding_id": {"S": _FID}, "finding_kind": {"S": _KIND}, "severity": {"S": "p0"},
        "headline": {"S": "Brake system: critical fault code C1234 is active"},
        "detail": {"S": _DIRECTIVE},
        "agent_id": {"S": "agent-2-vehicle-health"},
    }
    item = broker._deserialise_ddb_item(new_image)
    assert "finding" not in item
    _, kwargs = _call_send_apns_push(item)
    apns = json.loads(_parse_message(kwargs)["APNS"])
    assert apns["aps"]["alert"] == {"title": "Critical vehicle alert", "body": _DIRECTIVE}
