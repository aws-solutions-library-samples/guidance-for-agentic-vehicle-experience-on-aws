"""The seam between the diagnostics writer and the broker's push text, composed.

Spec 2026-11-05-avx-proactive-p0-alert § P. Each half is pinned on its own
(``agents/health/tests/test_diagnostics_p0_directive.py``,
``test_avx_broker_p0_directive_text.py``); this runs them in sequence on one value so a field
the writer stores under one name and the broker reads under another (the first T1.2a draft
read ``finding``, which the stored Finding does not have) cannot pass both halves.

C1234 → ``build_fields`` → the runner's ``_build_artifact`` → only the schema's properties
(what the writer stores) → ``_send_apns_push`` → the APNs body must be the catalog directive.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from unittest.mock import MagicMock, patch

_REPO = pathlib.Path(__file__).resolve().parents[4]
for p in (str(_REPO), str(_REPO / "lambdas"), os.path.join(os.path.dirname(__file__), "..")):
    if p not in sys.path:
        sys.path.insert(0, p)

import avx_broker as broker  # noqa: E402
from agents.supervisor.dtc_response_catalog import faults_for_codes, p0_directive  # noqa: E402


def _stored_finding_for(code: str) -> dict:
    from agents.health.cms_dtc_source import _level_and_system, build_fields
    from agents.tier2.runner import _build_artifact

    level, system = _level_and_system(code)
    fields = build_fields("brakes", [{"code": code, "first_seen_at": "2026-11-05", "description": "d",
                                      "system": system, "level": level}])
    artifact, _ = _build_artifact("1HGCM82633A004352", "brakes", [{"code": code}], fields,
                                  inputs_hash="h" * 64, ttl_seconds=1800)
    schema = json.loads((_REPO / "agents/tier2/schemas/finding.schema.json").read_text())
    return {k: v for k, v in artifact.items() if k in schema["properties"]}


def _published_apns(finding: dict) -> dict:
    table = MagicMock()
    table.query.return_value = {"Items": [{"owner_id": "o", "endpoint_arn": "arn:aws:sns:us-west-2:123456789012:endpoint/APNS_SANDBOX/a/b"}]}
    ddb = MagicMock()
    ddb.Table.return_value = table
    sns = MagicMock()
    with patch.object(broker, "_get_ddb", return_value=ddb), patch.object(broker, "_get_sns", return_value=sns):
        assert broker._send_apns_push("o", finding) == "published"
    return json.loads(json.loads(sns.publish.call_args[1]["Message"])["APNS"])


def test_c1234_written_then_pushed_carries_the_directive_verbatim():
    stored = _stored_finding_for("C1234")
    assert stored["severity"] == "p0"
    assert "finding" not in stored, "the schema has no 'finding' property; the writer does not store it"
    apns = _published_apns(stored)
    assert apns["aps"]["alert"] == {
        "title": "Critical vehicle alert",
        "body": p0_directive(faults_for_codes(["C1234"]), None),
    }
    assert apns["aps"]["category"] == "AVX_SAFETY"
    assert apns["open"] == "assistant"
