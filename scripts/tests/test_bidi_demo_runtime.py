"""Tests for scripts/bidi-demo-runtime.py — botocore Stubber, no live AWS calls.

Coverage:
  * plan sends no mutating API calls
  * guard_target_name refuses staging name and staging id-shaped string
  * guard_target_name accepts demo name
  * build_create_request copies every expected field from staging config
  * build_create_request sets a demo-specific description (W5)
  * build_update_artifact_request keeps all non-artifact demo fields (push)
  * build_update_artifact_request refuses wrong S3 prefix/bucket (W1)
  * upload_artifact refuses wrong S3 prefix/bucket (W1)
  * guard_jwt_enforce refuses non-enforce modes
  * wait_ready raises on any *_FAILED status (W2)
  * teardown always purges S3 (even when runtime absent) (W6)
  * teardown raises on delete_objects Errors (W6)
  * teardown polls until runtime gone (W6)
  * teardown deletes log group (W6)
  * _require_botocore_metadata raises when field absent (W4)
  * Stubber tests: create/push/sync-config --apply, guard, teardown-absent (W7)
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

# ---------------------------------------------------------------------------
# Load the module under test (hyphenated filename prevents normal import).
# ---------------------------------------------------------------------------
_SCRIPT_PATH = Path(__file__).parent.parent / "bidi-demo-runtime.py"
_spec = importlib.util.spec_from_file_location("bidi_demo_runtime", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

STAGING_NAME = _mod.STAGING_NAME
DEMO_NAME = _mod.DEMO_NAME
STAGING_REGION = _mod.STAGING_REGION

# ---------------------------------------------------------------------------
# Shared fake runtime fixtures
# ---------------------------------------------------------------------------

# Realistic staging runtime config (env values use real keys, synthetic values).
_STAGING_ID = "vsa_supervisor_bidi_staging-TESTXXXX"
_STAGING_ARN = (
    f"arn:aws:bedrock-agentcore:{STAGING_REGION}:123456789012:"
    f"runtime/{_STAGING_ID}"
)
_BUCKET = "vsa-staging-agentcore-codebuild-123456789012-us-west-2"
_STAGING_KEY = f"{STAGING_NAME}/deployment.zip"
_DEMO_KEY = f"{DEMO_NAME}/deployment.zip"
_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)

_STAGING_RUNTIME: dict = {
    "agentRuntimeId": _STAGING_ID,
    "agentRuntimeArn": _STAGING_ARN,
    "agentRuntimeName": STAGING_NAME,
    "agentRuntimeVersion": "91",
    "status": "READY",
    "createdAt": _NOW,
    "lastUpdatedAt": _NOW,
    "roleArn": (
        "arn:aws:iam::123456789012:role/"
        "AmazonBedrockAgentCoreSDKRuntime-us-west-2-staging"
    ),
    "networkConfiguration": {"networkMode": "PUBLIC"},
    "protocolConfiguration": {"serverProtocol": "HTTP"},
    "lifecycleConfiguration": {
        "idleRuntimeSessionTimeout": 900,
        "maxLifetime": 28800,
    },
    "description": "Test staging description",
    "environmentVariables": {
        "VSA_JWT_VERIFY_MODE": "enforce",
        "VSA_STAGE": "staging",
        "VSA_API_BASE": "https://api.example.com",
        "AWS_REGION": "us-west-2",
    },
    "requestHeaderConfiguration": {
        "requestHeaderAllowlist": [
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id",
        ]
    },
    "agentRuntimeArtifact": {
        "codeConfiguration": {
            "code": {
                "s3": {
                    "bucket": _BUCKET,
                    "prefix": _STAGING_KEY,
                }
            },
            "runtime": "PYTHON_3_12",
            "entryPoint": ["agents/supervisor/bidi_app.py"],
        }
    },
    "metadataConfiguration": {"requireMMDSV2": True},
}

_DEMO_ID = "vsa_supervisor_bidi_demo_staging-TESTXXXX"
_DEMO_ARN = (
    f"arn:aws:bedrock-agentcore:{STAGING_REGION}:123456789012:"
    f"runtime/{_DEMO_ID}"
)
_DEMO_DESCRIPTION = (
    "Recording runtime; config copied from vsa_supervisor_bidi_staging v91 at 2026-10-30T12:00:00Z"
)

_DEMO_RUNTIME: dict = {
    **_STAGING_RUNTIME,
    "agentRuntimeId": _DEMO_ID,
    "agentRuntimeArn": _DEMO_ARN,
    "agentRuntimeName": DEMO_NAME,
    "agentRuntimeVersion": "3",
    "description": _DEMO_DESCRIPTION,
    "agentRuntimeArtifact": {
        "codeConfiguration": {
            "code": {
                "s3": {
                    "bucket": _BUCKET,
                    "prefix": _DEMO_KEY,
                    "versionId": "demo-ver-001",
                }
            },
            "runtime": "PYTHON_3_12",
            "entryPoint": ["agents/supervisor/bidi_app.py"],
        }
    },
}

# ---------------------------------------------------------------------------
# Helpers: build Stubber-backed clients with fake credentials.
# ---------------------------------------------------------------------------

def _control_client():
    return boto3.client(
        "bedrock-agentcore-control",
        region_name=STAGING_REGION,
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        aws_session_token="test-token",
    )


def _s3_client():
    return boto3.client(
        "s3",
        region_name=STAGING_REGION,
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        aws_session_token="test-token",
    )


def _logs_client():
    return boto3.client(
        "logs",
        region_name=STAGING_REGION,
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        aws_session_token="test-token",
    )


def _runtime_summary(rt: dict) -> dict:
    """Build the abbreviated summary entry that list_agent_runtimes returns."""
    return {
        "agentRuntimeArn": rt["agentRuntimeArn"],
        "agentRuntimeId": rt["agentRuntimeId"],
        "agentRuntimeVersion": rt["agentRuntimeVersion"],
        "agentRuntimeName": rt["agentRuntimeName"],
        "description": rt.get("description", ""),
        "lastUpdatedAt": rt.get("lastUpdatedAt", _NOW),
        "status": rt.get("status", "READY"),
    }


def _queue_list_finds(stubber: Stubber, *runtimes) -> None:
    """Queue a single list_agent_runtimes response returning *runtimes*."""
    stubber.add_response(
        "list_agent_runtimes",
        {"agentRuntimes": [_runtime_summary(rt) for rt in runtimes]},
        expected_params={},
    )


def _queue_list_empty(stubber: Stubber) -> None:
    """Queue a list_agent_runtimes response with no runtimes."""
    stubber.add_response(
        "list_agent_runtimes",
        {"agentRuntimes": []},
        expected_params={},
    )


def _queue_get(stubber: Stubber, rt: dict) -> None:
    """Queue a get_agent_runtime response for the given runtime."""
    stubber.add_response(
        "get_agent_runtime",
        rt,
        expected_params={"agentRuntimeId": rt["agentRuntimeId"]},
    )


# ---------------------------------------------------------------------------
# Tests: botocore compatibility gate (W4)
# ---------------------------------------------------------------------------

class TestBotocoreGate:
    """_require_botocore_metadata refuses if botocore lacks metadataConfiguration."""

    def test_raises_when_metadataConfiguration_absent(self, monkeypatch):
        """Raises RuntimeError when the service model lacks metadataConfiguration."""
        import botocore.session

        class _FakeMembers:
            """dict-like that never contains metadataConfiguration."""
            def __contains__(self, key):
                return key != "metadataConfiguration"

        class _FakeShape:
            members = _FakeMembers()

        class _FakeOp:
            input_shape = _FakeShape()

        class _FakeSvc:
            def operation_model(self, name):
                return _FakeOp()

        monkeypatch.setattr(
            botocore.session.Session,
            "get_service_model",
            lambda self, name: _FakeSvc(),
        )
        with pytest.raises(RuntimeError, match="botocore"):
            _mod._require_botocore_metadata()

    def test_passes_on_current_venv(self):
        """Does not raise on the AVX venv (botocore >= 1.43.6) — module already loaded."""
        _mod._require_botocore_metadata()  # must not raise


# ---------------------------------------------------------------------------
# Tests: guard functions (unit)
# ---------------------------------------------------------------------------

class TestGuardTargetName:
    """guard_target_name accepts only DEMO_NAME."""

    def test_accepts_demo_name(self):
        _mod.guard_target_name(DEMO_NAME)  # must not raise

    def test_rejects_staging_name(self):
        with pytest.raises(ValueError, match=DEMO_NAME):
            _mod.guard_target_name(STAGING_NAME)

    def test_rejects_staging_id_string(self):
        """A staging runtime ID (contains staging name) is also refused."""
        with pytest.raises(ValueError, match=DEMO_NAME):
            _mod.guard_target_name(_STAGING_ID)

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            _mod.guard_target_name("")


class TestGuardJwtEnforce:
    """guard_jwt_enforce accepts 'enforce', refuses everything else."""

    def test_accepts_enforce(self):
        _mod.guard_jwt_enforce({"VSA_JWT_VERIFY_MODE": "enforce"})  # must not raise

    def test_rejects_passthrough(self):
        with pytest.raises(ValueError, match="enforce"):
            _mod.guard_jwt_enforce({"VSA_JWT_VERIFY_MODE": "passthrough"})

    def test_rejects_missing_key(self):
        with pytest.raises(ValueError, match="enforce"):
            _mod.guard_jwt_enforce({})

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="enforce"):
            _mod.guard_jwt_enforce({"VSA_JWT_VERIFY_MODE": ""})


class TestGuardRegion:
    """guard_region only accepts STAGING_REGION."""

    def test_accepts_staging_region(self):
        _mod.guard_region(STAGING_REGION)  # must not raise

    def test_rejects_other_region(self):
        with pytest.raises(ValueError, match=STAGING_REGION):
            _mod.guard_region("eu-west-1")


# ---------------------------------------------------------------------------
# Tests: wait_ready *_FAILED statuses (W2)
# ---------------------------------------------------------------------------

class TestWaitReadyFailed:
    """wait_ready raises RuntimeError on any *_FAILED status."""

    def _make_failed_runtime(self, status: str) -> dict:
        return {
            **_DEMO_RUNTIME,
            "status": status,
            "failureReason": f"Synthetic {status} for test",
        }

    def test_create_failed_raises(self):
        control = _control_client()
        stubber = Stubber(control)
        _queue_get(stubber, self._make_failed_runtime("CREATE_FAILED"))
        with stubber:
            with pytest.raises(RuntimeError, match="CREATE_FAILED"):
                _mod.wait_ready(control, _DEMO_ID)

    def test_update_failed_raises(self):
        control = _control_client()
        stubber = Stubber(control)
        _queue_get(stubber, self._make_failed_runtime("UPDATE_FAILED"))
        with stubber:
            with pytest.raises(RuntimeError, match="UPDATE_FAILED"):
                _mod.wait_ready(control, _DEMO_ID)

    def test_delete_failed_raises(self):
        control = _control_client()
        stubber = Stubber(control)
        _queue_get(stubber, self._make_failed_runtime("DELETE_FAILED"))
        with stubber:
            with pytest.raises(RuntimeError, match="DELETE_FAILED"):
                _mod.wait_ready(control, _DEMO_ID)

    def test_ready_returns_response(self):
        control = _control_client()
        stubber = Stubber(control)
        _queue_get(stubber, {**_DEMO_RUNTIME, "status": "READY"})
        with stubber:
            resp = _mod.wait_ready(control, _DEMO_ID)
        assert resp["status"] == "READY"


# ---------------------------------------------------------------------------
# Tests: S3 key guard — upload_artifact + build_update_artifact_request (W1)
# ---------------------------------------------------------------------------

class TestS3KeyGuard:
    """upload_artifact and build_update_artifact_request refuse wrong S3 prefix/bucket."""

    def test_upload_artifact_refuses_staging_prefix(self, tmp_path):
        """If demo artifact prefix is staging's key, upload is refused."""
        s3 = _s3_client()
        bad_demo = copy.deepcopy(_DEMO_RUNTIME)
        bad_demo["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["prefix"] = _STAGING_KEY

        zip_file = tmp_path / "deployment.zip"
        zip_file.write_bytes(b"fake")

        with pytest.raises(ValueError, match="upload_artifact refused"):
            _mod.upload_artifact(s3, bad_demo, zip_file, staging_bucket=_BUCKET)

    def test_upload_artifact_refuses_wrong_bucket(self, tmp_path):
        """If demo artifact bucket differs from staging_bucket, upload is refused."""
        s3 = _s3_client()
        zip_file = tmp_path / "deployment.zip"
        zip_file.write_bytes(b"fake")

        with pytest.raises(ValueError, match="upload_artifact refused"):
            _mod.upload_artifact(s3, _DEMO_RUNTIME, zip_file, staging_bucket="wrong-bucket")

    def test_upload_artifact_accepts_correct_key(self, tmp_path):
        """Correct prefix + bucket lets the upload proceed."""
        s3 = _s3_client()
        s3_stub = Stubber(s3)
        zip_content = b"fake-zip"
        s3_stub.add_response(
            "put_object",
            {"VersionId": "v-new"},
            expected_params={"Bucket": _BUCKET, "Key": _DEMO_KEY, "Body": zip_content},
        )
        zip_file = tmp_path / "deployment.zip"
        zip_file.write_bytes(zip_content)

        with s3_stub:
            bucket, prefix, version_id = _mod.upload_artifact(
                s3, _DEMO_RUNTIME, zip_file, staging_bucket=_BUCKET
            )

        assert prefix == _DEMO_KEY
        assert version_id == "v-new"

    def test_build_update_artifact_request_refuses_staging_prefix(self):
        bad_demo = copy.deepcopy(_DEMO_RUNTIME)
        bad_demo["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["prefix"] = _STAGING_KEY

        with pytest.raises(ValueError, match="build_update_artifact_request refused"):
            _mod.build_update_artifact_request(_DEMO_ID, bad_demo, "v1", staging_bucket=_BUCKET)

    def test_build_update_artifact_request_refuses_wrong_bucket(self):
        with pytest.raises(ValueError, match="build_update_artifact_request refused"):
            _mod.build_update_artifact_request(
                _DEMO_ID, _DEMO_RUNTIME, "v1", staging_bucket="wrong-bucket"
            )

    def test_build_update_artifact_request_accepts_correct_key(self):
        req = _mod.build_update_artifact_request(
            _DEMO_ID, _DEMO_RUNTIME, "v-ok", staging_bucket=_BUCKET
        )
        s3 = req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["prefix"] == _DEMO_KEY
        assert s3["versionId"] == "v-ok"


# ---------------------------------------------------------------------------
# Tests: request builders (unit)
# ---------------------------------------------------------------------------

class TestBuildCreateRequest:
    """build_create_request copies every expected field from staging."""

    def setup_method(self):
        self.req = _mod.build_create_request(_STAGING_RUNTIME, version_id="v-abc123")

    def test_name_is_demo(self):
        assert self.req["agentRuntimeName"] == DEMO_NAME

    def test_s3_prefix_is_demo(self):
        s3 = self.req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["prefix"] == _DEMO_KEY

    def test_s3_bucket_matches_staging(self):
        s3 = self.req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["bucket"] == _BUCKET

    def test_s3_version_id_set(self):
        s3 = self.req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["versionId"] == "v-abc123"

    def test_runtime_copied(self):
        cc = self.req["agentRuntimeArtifact"]["codeConfiguration"]
        assert cc["runtime"] == "PYTHON_3_12"

    def test_entry_point_copied(self):
        cc = self.req["agentRuntimeArtifact"]["codeConfiguration"]
        assert cc["entryPoint"] == ["agents/supervisor/bidi_app.py"]

    def test_role_arn_copied(self):
        assert self.req["roleArn"] == _STAGING_RUNTIME["roleArn"]

    def test_network_configuration_copied(self):
        assert self.req["networkConfiguration"] == {"networkMode": "PUBLIC"}

    def test_protocol_configuration_copied(self):
        assert self.req["protocolConfiguration"] == {"serverProtocol": "HTTP"}

    def test_lifecycle_configuration_copied(self):
        lc = self.req["lifecycleConfiguration"]
        assert lc["idleRuntimeSessionTimeout"] == 900
        assert lc["maxLifetime"] == 28800

    def test_environment_variables_copied(self):
        env = self.req["environmentVariables"]
        assert env["VSA_JWT_VERIFY_MODE"] == "enforce"
        assert env["VSA_STAGE"] == "staging"

    def test_header_allowlist_copied(self):
        headers = self.req["requestHeaderConfiguration"]["requestHeaderAllowlist"]
        assert "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token" in headers

    def test_description_is_demo_specific(self):
        """description is NOT staging's verbatim description — it's a recording note."""
        desc = self.req["description"]
        assert "Recording runtime" in desc
        assert STAGING_NAME in desc
        assert _STAGING_RUNTIME["agentRuntimeVersion"] in desc
        assert desc != _STAGING_RUNTIME["description"]

    def test_metadataConfiguration_not_in_create(self):
        # metadataConfiguration is not in CreateAgentRuntime input shape.
        assert "metadataConfiguration" not in self.req

    def test_version_id_none_omits_field(self):
        req = _mod.build_create_request(_STAGING_RUNTIME, version_id=None)
        s3 = req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert "versionId" not in s3


class TestBuildUpdateArtifactRequest:
    """build_update_artifact_request (push) keeps non-artifact fields from demo."""

    def setup_method(self):
        self.req = _mod.build_update_artifact_request(
            _DEMO_ID, _DEMO_RUNTIME, new_version_id="v-new-999", staging_bucket=_BUCKET
        )

    def test_target_id_is_demo(self):
        assert self.req["agentRuntimeId"] == _DEMO_ID

    def test_new_version_id_set(self):
        s3 = self.req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["versionId"] == "v-new-999"

    def test_demo_prefix_preserved(self):
        s3 = self.req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["prefix"] == _DEMO_KEY

    def test_lifecycle_from_demo(self):
        """push keeps demo's current lifecycle (not staging's)."""
        lc = self.req["lifecycleConfiguration"]
        assert lc["idleRuntimeSessionTimeout"] == 900

    def test_env_from_demo(self):
        env = self.req["environmentVariables"]
        assert "VSA_JWT_VERIFY_MODE" in env

    def test_header_allowlist_from_demo(self):
        headers = self.req["requestHeaderConfiguration"]["requestHeaderAllowlist"]
        assert len(headers) >= 1

    def test_no_agent_runtime_name_field(self):
        # UpdateAgentRuntime does not accept agentRuntimeName.
        assert "agentRuntimeName" not in self.req


class TestBuildUpdateConfigRequest:
    """build_update_config_request (sync-config) sets demo description and includes metadataConfiguration."""

    def setup_method(self):
        self.req = _mod.build_update_config_request(
            _DEMO_ID, _STAGING_RUNTIME, _DEMO_RUNTIME["agentRuntimeArtifact"]
        )

    def test_target_id_is_demo(self):
        assert self.req["agentRuntimeId"] == _DEMO_ID

    def test_artifact_unchanged(self):
        assert self.req["agentRuntimeArtifact"] == _DEMO_RUNTIME["agentRuntimeArtifact"]

    def test_metadataConfiguration_included(self):
        assert self.req["metadataConfiguration"] == {"requireMMDSV2": True}

    def test_description_is_demo_specific(self):
        """sync-config sets a fresh demo description, not staging's verbatim."""
        desc = self.req.get("description", "")
        assert "Recording runtime" in desc
        assert STAGING_NAME in desc
        assert desc != _STAGING_RUNTIME["description"]

    def test_no_agentRuntimeName(self):
        assert "agentRuntimeName" not in self.req


# ---------------------------------------------------------------------------
# Tests: plan sends no mutating calls
# ---------------------------------------------------------------------------

class TestPlanNoMutations:
    """cmd_plan makes only list + get calls; Stubber raises on any unexpected call."""

    def test_plan_returns_zero_and_makes_no_mutations(self, capsys):
        control = _control_client()
        stubber = Stubber(control)

        _queue_list_finds(stubber, _STAGING_RUNTIME)
        _queue_get(stubber, _STAGING_RUNTIME)
        _queue_list_empty(stubber)  # demo lookup: nothing found

        with stubber:
            rc = _mod.cmd_plan(control)

        assert rc == 0
        stubber.assert_no_pending_responses()

    def test_plan_prints_demo_name(self, capsys):
        control = _control_client()
        stubber = Stubber(control)
        _queue_list_finds(stubber, _STAGING_RUNTIME)
        _queue_get(stubber, _STAGING_RUNTIME)
        _queue_list_empty(stubber)

        with stubber:
            _mod.cmd_plan(control)

        out = capsys.readouterr().out
        assert DEMO_NAME in out

    def test_plan_prints_env_names_not_values(self, capsys):
        control = _control_client()
        stubber = Stubber(control)
        _queue_list_finds(stubber, _STAGING_RUNTIME)
        _queue_get(stubber, _STAGING_RUNTIME)
        _queue_list_empty(stubber)

        with stubber:
            _mod.cmd_plan(control)

        out = capsys.readouterr().out
        assert "VSA_JWT_VERIFY_MODE" in out
        assert "https://api.example.com" not in out

    def test_plan_shows_staging_name_exists_when_demo_exists(self, capsys):
        control = _control_client()
        stubber = Stubber(control)
        _queue_list_finds(stubber, _STAGING_RUNTIME)
        _queue_get(stubber, _STAGING_RUNTIME)
        # Demo lookup: finds demo
        _queue_list_finds(stubber, _STAGING_RUNTIME, _DEMO_RUNTIME)
        _queue_get(stubber, _DEMO_RUNTIME)

        with stubber:
            rc = _mod.cmd_plan(control)

        assert rc == 0
        out = capsys.readouterr().out
        assert "already EXISTS" in out

    def test_plan_prints_arn_when_demo_exists(self, capsys):
        """cmd_plan prints the demo ARN when the demo is already running (Suggestion)."""
        control = _control_client()
        stubber = Stubber(control)
        _queue_list_finds(stubber, _STAGING_RUNTIME)
        _queue_get(stubber, _STAGING_RUNTIME)
        _queue_list_finds(stubber, _STAGING_RUNTIME, _DEMO_RUNTIME)
        _queue_get(stubber, _DEMO_RUNTIME)

        with stubber:
            _mod.cmd_plan(control)

        out = capsys.readouterr().out
        assert _DEMO_ARN in out


# ---------------------------------------------------------------------------
# Tests: enforce check blocks create and sync-config
# ---------------------------------------------------------------------------

class TestEnforceCheck:
    """create and sync-config refuse when staging has non-enforce JWT mode."""

    def _staging_passthrough(self) -> dict:
        s = dict(_STAGING_RUNTIME)
        s["environmentVariables"] = {
            **_STAGING_RUNTIME["environmentVariables"],
            "VSA_JWT_VERIFY_MODE": "passthrough",
        }
        return s

    def test_create_refuses_passthrough(self):
        staging = self._staging_passthrough()
        with pytest.raises(ValueError, match="enforce"):
            _mod.guard_jwt_enforce(staging["environmentVariables"])

    def test_cmd_create_dry_run_exits_1_on_passthrough(self, capsys):
        control = _control_client()
        stubber = Stubber(control)
        staging_pt = self._staging_passthrough()

        _queue_list_finds(stubber, _STAGING_RUNTIME)
        _queue_get(stubber, staging_pt)

        with stubber:
            with pytest.raises(ValueError, match="enforce"):
                _mod.cmd_create(control, None, apply=False)

    def test_cmd_sync_config_raises_on_passthrough(self, capsys):
        control = _control_client()
        stubber = Stubber(control)
        staging_pt = self._staging_passthrough()

        _queue_list_finds(stubber, _STAGING_RUNTIME)
        _queue_get(stubber, staging_pt)

        with stubber:
            with pytest.raises(ValueError, match="enforce"):
                _mod.cmd_sync_config(control, apply=False)


# ---------------------------------------------------------------------------
# Tests: teardown (W6 — always purge S3, raise on errors, poll, log group)
# ---------------------------------------------------------------------------

class TestTeardownDeletesAllVersions:
    """cmd_teardown deletes runtime, polls until gone, purges S3, deletes log group."""

    def test_teardown_dry_run_makes_no_s3_calls(self, capsys):
        control = _control_client()
        s3 = _s3_client()
        logs = _logs_client()
        ctrl_stub = Stubber(control)
        s3_stub = Stubber(s3)
        logs_stub = Stubber(logs)

        _queue_list_finds(ctrl_stub, _DEMO_RUNTIME)
        _queue_get(ctrl_stub, _DEMO_RUNTIME)

        with ctrl_stub, s3_stub, logs_stub:
            rc = _mod.cmd_teardown(control, s3, logs, apply=False)

        assert rc == 0
        s3_stub.assert_no_pending_responses()
        logs_stub.assert_no_pending_responses()

    def test_teardown_deletes_two_versions_and_one_delete_marker(self):
        control = _control_client()
        s3 = _s3_client()
        logs = _logs_client()
        ctrl_stub = Stubber(control)
        s3_stub = Stubber(s3)
        logs_stub = Stubber(logs)

        s3_prefix = f"{DEMO_NAME}/"
        log_group = f"/aws/bedrock-agentcore/runtimes/{_DEMO_ID}-DEFAULT"

        # _get_demo_full: list → get
        _queue_list_finds(ctrl_stub, _DEMO_RUNTIME)
        _queue_get(ctrl_stub, _DEMO_RUNTIME)

        # delete_agent_runtime
        ctrl_stub.add_response(
            "delete_agent_runtime",
            {"status": "DELETING", "agentRuntimeId": _DEMO_ID},
            expected_params={"agentRuntimeId": _DEMO_ID},
        )

        # Poll: list_agent_runtimes returns empty → runtime gone
        _queue_list_empty(ctrl_stub)

        # list_object_versions: 2 versions + 1 delete marker, not truncated
        s3_stub.add_response(
            "list_object_versions",
            {
                "Versions": [
                    {
                        "Key": f"{DEMO_NAME}/deployment.zip",
                        "VersionId": "ver-001",
                        "IsLatest": True,
                        "LastModified": _NOW,
                        "ETag": '"aaa"',
                        "Size": 1024,
                        "StorageClass": "STANDARD",
                        "Owner": {"DisplayName": "test", "ID": "id"},
                    },
                    {
                        "Key": f"{DEMO_NAME}/deployment.zip",
                        "VersionId": "ver-002",
                        "IsLatest": False,
                        "LastModified": _NOW,
                        "ETag": '"bbb"',
                        "Size": 1000,
                        "StorageClass": "STANDARD",
                        "Owner": {"DisplayName": "test", "ID": "id"},
                    },
                ],
                "DeleteMarkers": [
                    {
                        "Key": f"{DEMO_NAME}/deployment.zip",
                        "VersionId": "dm-001",
                        "IsLatest": False,
                        "LastModified": _NOW,
                        "Owner": {"DisplayName": "test", "ID": "id"},
                    }
                ],
                "IsTruncated": False,
                "Name": _BUCKET,
                "Prefix": s3_prefix,
                "MaxKeys": 1000,
            },
            expected_params={"Bucket": _BUCKET, "Prefix": s3_prefix},
        )

        # delete_objects — all three
        s3_stub.add_response(
            "delete_objects",
            {"Deleted": [], "Errors": []},
            expected_params={
                "Bucket": _BUCKET,
                "Delete": {
                    "Objects": [
                        {"Key": f"{DEMO_NAME}/deployment.zip", "VersionId": "ver-001"},
                        {"Key": f"{DEMO_NAME}/deployment.zip", "VersionId": "ver-002"},
                        {"Key": f"{DEMO_NAME}/deployment.zip", "VersionId": "dm-001"},
                    ],
                    "Quiet": True,
                },
            },
        )

        # delete_log_group
        logs_stub.add_response(
            "delete_log_group",
            {},
            expected_params={"logGroupName": log_group},
        )

        with ctrl_stub, s3_stub, logs_stub:
            rc = _mod.cmd_teardown(control, s3, logs, apply=True)

        assert rc == 0
        ctrl_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()
        logs_stub.assert_no_pending_responses()

    def test_teardown_s3_purged_when_demo_absent(self):
        """When demo is absent, teardown still purges S3 using staging's bucket (W6)."""
        control = _control_client()
        s3 = _s3_client()
        logs = _logs_client()
        ctrl_stub = Stubber(control)
        s3_stub = Stubber(s3)

        s3_prefix = f"{DEMO_NAME}/"

        # _get_demo_full: list empty
        _queue_list_empty(ctrl_stub)

        # _get_staging_full (to get bucket)
        _queue_list_finds(ctrl_stub, _STAGING_RUNTIME)
        _queue_get(ctrl_stub, _STAGING_RUNTIME)

        # S3 list: no versions at all
        s3_stub.add_response(
            "list_object_versions",
            {
                "Versions": [],
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Name": _BUCKET,
                "Prefix": s3_prefix,
                "MaxKeys": 1000,
            },
            expected_params={"Bucket": _BUCKET, "Prefix": s3_prefix},
        )

        with ctrl_stub, s3_stub:
            rc = _mod.cmd_teardown(control, s3, logs, apply=True)

        assert rc == 0
        ctrl_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()

    def test_teardown_raises_on_delete_objects_errors(self):
        """delete_all_s3_versions raises RuntimeError when delete_objects reports errors (W6)."""
        s3 = _s3_client()
        s3_stub = Stubber(s3)

        s3_prefix = f"{DEMO_NAME}/"
        s3_stub.add_response(
            "list_object_versions",
            {
                "Versions": [
                    {
                        "Key": f"{DEMO_NAME}/deployment.zip",
                        "VersionId": "ver-001",
                        "IsLatest": True,
                        "LastModified": _NOW,
                        "ETag": '"aaa"',
                        "Size": 1024,
                        "StorageClass": "STANDARD",
                        "Owner": {"DisplayName": "test", "ID": "id"},
                    }
                ],
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Name": _BUCKET,
                "Prefix": s3_prefix,
                "MaxKeys": 1000,
            },
            expected_params={"Bucket": _BUCKET, "Prefix": s3_prefix},
        )
        s3_stub.add_response(
            "delete_objects",
            {
                "Deleted": [],
                "Errors": [
                    {
                        "Key": f"{DEMO_NAME}/deployment.zip",
                        "VersionId": "ver-001",
                        "Code": "InternalError",
                        "Message": "synthetic error",
                    }
                ],
            },
            expected_params={
                "Bucket": _BUCKET,
                "Delete": {
                    "Objects": [{"Key": f"{DEMO_NAME}/deployment.zip", "VersionId": "ver-001"}],
                    "Quiet": True,
                },
            },
        )

        with s3_stub:
            with pytest.raises(RuntimeError, match="error"):
                _mod.delete_all_s3_versions(s3, _BUCKET, s3_prefix)

    def test_teardown_raises_on_delete_failed_during_poll(self):
        """If runtime enters DELETE_FAILED during poll, teardown raises (W6)."""
        control = _control_client()
        s3 = _s3_client()
        logs = _logs_client()
        ctrl_stub = Stubber(control)

        _queue_list_finds(ctrl_stub, _DEMO_RUNTIME)
        _queue_get(ctrl_stub, _DEMO_RUNTIME)

        ctrl_stub.add_response(
            "delete_agent_runtime",
            {"status": "DELETING", "agentRuntimeId": _DEMO_ID},
            expected_params={"agentRuntimeId": _DEMO_ID},
        )

        # Poll returns DELETE_FAILED
        ctrl_stub.add_response(
            "list_agent_runtimes",
            {"agentRuntimes": [_runtime_summary({**_DEMO_RUNTIME, "status": "DELETE_FAILED"})]},
            expected_params={},
        )

        with ctrl_stub:
            with pytest.raises(RuntimeError, match="DELETE_FAILED"):
                _mod.cmd_teardown(control, s3, logs, apply=True)


# ---------------------------------------------------------------------------
# Tests: Stubber --apply paths (W7)
# ---------------------------------------------------------------------------

class TestStubberApply:
    """Stubber tests for write subcommands with --apply — verify key API calls."""

    def test_push_rejects_staging_named_runtime(self, tmp_path):
        """cmd_push with a staging-named demo runtime sends no put_object or update (W7)."""
        control = _control_client()
        s3 = _s3_client()
        ctrl_stub = Stubber(control)
        s3_stub = Stubber(s3)

        # list finds demo id, but get returns staging name (simulates guard scenario)
        _queue_list_finds(ctrl_stub, _DEMO_RUNTIME)
        staging_named = dict(_DEMO_RUNTIME)
        staging_named["agentRuntimeName"] = STAGING_NAME
        _queue_get(ctrl_stub, staging_named)

        zip_file = tmp_path / "deployment.zip"
        zip_file.write_bytes(b"fake")

        with ctrl_stub, s3_stub:
            with pytest.raises(ValueError, match=DEMO_NAME):
                _mod.cmd_push(control, s3, zip_path=zip_file, apply=False)

        s3_stub.assert_no_pending_responses()  # no S3 calls made

    def test_create_apply_sends_copy_to_demo_key(self):
        """create --apply copies to the demo S3 key, not staging's key (W7)."""
        control = _control_client()
        s3 = _s3_client()
        ctrl_stub = Stubber(control)
        s3_stub = Stubber(s3)

        # _get_staging_full
        _queue_list_finds(ctrl_stub, _STAGING_RUNTIME)
        _queue_get(ctrl_stub, _STAGING_RUNTIME)

        # _get_demo_full → empty
        _queue_list_empty(ctrl_stub)

        # copy_object: Key MUST be _DEMO_KEY, not _STAGING_KEY
        s3_stub.add_response(
            "copy_object",
            {"VersionId": "demo-ver-001"},
            expected_params={
                "Bucket": _BUCKET,
                "Key": _DEMO_KEY,                         # asserts demo key used
                "CopySource": {"Bucket": _BUCKET, "Key": _STAGING_KEY},
            },
        )

        # create_agent_runtime — botocore validates params against service model
        ctrl_stub.add_response(
            "create_agent_runtime",
            {
                "agentRuntimeId": _DEMO_ID,
                "agentRuntimeArn": _DEMO_ARN,
                "agentRuntimeVersion": "1",
                "status": "CREATING",
                "createdAt": _NOW,
            },
        )

        # wait_ready: poll returns READY
        _queue_get(ctrl_stub, {**_DEMO_RUNTIME, "status": "READY", "agentRuntimeVersion": "1"})

        # post-create update (metadataConfiguration) — botocore validates params
        ctrl_stub.add_response(
            "update_agent_runtime",
            {
                "agentRuntimeId": _DEMO_ID,
                "agentRuntimeArn": _DEMO_ARN,
                "agentRuntimeVersion": "2",
                "status": "UPDATING",
                "createdAt": _NOW,
                "lastUpdatedAt": _NOW,
            },
        )

        # second wait_ready poll
        _queue_get(ctrl_stub, {**_DEMO_RUNTIME, "status": "READY", "agentRuntimeVersion": "2"})

        with ctrl_stub, s3_stub:
            rc = _mod.cmd_create(control, s3, apply=True)

        assert rc == 0
        ctrl_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()

    def test_push_apply_sends_update_to_demo_id(self, tmp_path):
        """push --apply sends update_agent_runtime with agentRuntimeId == demo id (W7)."""
        control = _control_client()
        s3 = _s3_client()
        ctrl_stub = Stubber(control)
        s3_stub = Stubber(s3)

        zip_content = b"fake-zip"
        zip_file = tmp_path / "deployment.zip"
        zip_file.write_bytes(zip_content)

        # _get_demo_full
        _queue_list_finds(ctrl_stub, _DEMO_RUNTIME)
        _queue_get(ctrl_stub, _DEMO_RUNTIME)

        # _get_staging_full (for bucket guard)
        _queue_list_finds(ctrl_stub, _STAGING_RUNTIME)
        _queue_get(ctrl_stub, _STAGING_RUNTIME)

        # put_object
        s3_stub.add_response(
            "put_object",
            {"VersionId": "pushed-ver"},
            expected_params={"Bucket": _BUCKET, "Key": _DEMO_KEY, "Body": zip_content},
        )

        # update_agent_runtime: agentRuntimeId must be demo id, not staging id
        ctrl_stub.add_response(
            "update_agent_runtime",
            {
                "agentRuntimeId": _DEMO_ID,
                "agentRuntimeArn": _DEMO_ARN,
                "agentRuntimeVersion": "4",
                "status": "UPDATING",
                "createdAt": _NOW,
                "lastUpdatedAt": _NOW,
            },
        )

        # wait_ready poll
        _queue_get(ctrl_stub, {**_DEMO_RUNTIME, "status": "READY", "agentRuntimeVersion": "4"})

        with ctrl_stub, s3_stub:
            rc = _mod.cmd_push(control, s3, zip_path=zip_file, apply=True)

        assert rc == 0
        ctrl_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()

    def test_sync_config_apply_sends_update_to_demo_id(self):
        """sync-config --apply sends update_agent_runtime with agentRuntimeId == demo id (W7)."""
        control = _control_client()
        ctrl_stub = Stubber(control)

        # _get_staging_full
        _queue_list_finds(ctrl_stub, _STAGING_RUNTIME)
        _queue_get(ctrl_stub, _STAGING_RUNTIME)

        # _get_demo_full
        _queue_list_finds(ctrl_stub, _DEMO_RUNTIME)
        _queue_get(ctrl_stub, _DEMO_RUNTIME)

        # update_agent_runtime: agentRuntimeId must be demo id
        ctrl_stub.add_response(
            "update_agent_runtime",
            {
                "agentRuntimeId": _DEMO_ID,
                "agentRuntimeArn": _DEMO_ARN,
                "agentRuntimeVersion": "4",
                "status": "UPDATING",
                "createdAt": _NOW,
                "lastUpdatedAt": _NOW,
            },
        )

        # wait_ready poll
        _queue_get(ctrl_stub, {**_DEMO_RUNTIME, "status": "READY", "agentRuntimeVersion": "4"})

        with ctrl_stub:
            rc = _mod.cmd_sync_config(control, apply=True)

        assert rc == 0
        ctrl_stub.assert_no_pending_responses()

    def test_teardown_absent_runtime_purges_s3(self):
        """Teardown with absent runtime still purges S3 (W7)."""
        control = _control_client()
        s3 = _s3_client()
        logs = _logs_client()
        ctrl_stub = Stubber(control)
        s3_stub = Stubber(s3)

        s3_prefix = f"{DEMO_NAME}/"

        # No demo
        _queue_list_empty(ctrl_stub)
        # Staging lookup for bucket
        _queue_list_finds(ctrl_stub, _STAGING_RUNTIME)
        _queue_get(ctrl_stub, _STAGING_RUNTIME)
        # S3 has one orphan version
        s3_stub.add_response(
            "list_object_versions",
            {
                "Versions": [
                    {
                        "Key": _DEMO_KEY,
                        "VersionId": "orphan-001",
                        "IsLatest": True,
                        "LastModified": _NOW,
                        "ETag": '"abc"',
                        "Size": 1024,
                        "StorageClass": "STANDARD",
                        "Owner": {"DisplayName": "test", "ID": "id"},
                    }
                ],
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Name": _BUCKET,
                "Prefix": s3_prefix,
                "MaxKeys": 1000,
            },
            expected_params={"Bucket": _BUCKET, "Prefix": s3_prefix},
        )
        s3_stub.add_response(
            "delete_objects",
            {"Deleted": [], "Errors": []},
            expected_params={
                "Bucket": _BUCKET,
                "Delete": {
                    "Objects": [{"Key": _DEMO_KEY, "VersionId": "orphan-001"}],
                    "Quiet": True,
                },
            },
        )

        with ctrl_stub, s3_stub:
            rc = _mod.cmd_teardown(control, s3, logs, apply=True)

        assert rc == 0
        ctrl_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Tests: every field copied (integration-style, no AWS calls)
# ---------------------------------------------------------------------------

class TestEveryFieldCopied:
    """Verifies that all documented fields are present in the create request."""

    _EXPECTED_CREATE_FIELDS = {
        "agentRuntimeName",
        "agentRuntimeArtifact",
        "roleArn",
        "networkConfiguration",
        "protocolConfiguration",
        "lifecycleConfiguration",
        "environmentVariables",
        "requestHeaderConfiguration",
        "description",
    }

    def test_all_expected_fields_present(self):
        req = _mod.build_create_request(_STAGING_RUNTIME, version_id="v-1")
        missing = self._EXPECTED_CREATE_FIELDS - req.keys()
        assert not missing, f"Missing fields in create request: {missing}"

    def test_staging_name_not_in_create_request(self):
        req = _mod.build_create_request(_STAGING_RUNTIME, version_id="v-1")
        assert req["agentRuntimeName"] != STAGING_NAME

    def test_staging_prefix_not_in_create_request(self):
        req = _mod.build_create_request(_STAGING_RUNTIME, version_id="v-1")
        prefix = req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["prefix"]
        assert prefix != _STAGING_KEY
        assert prefix == _DEMO_KEY


# ---------------------------------------------------------------------------
# Tests: push keeps non-artifact fields unchanged
# ---------------------------------------------------------------------------

class TestPushKeepsFields:
    """build_update_artifact_request preserves demo's current config fields."""

    def test_keeps_demo_env(self):
        demo_custom = dict(_DEMO_RUNTIME)
        demo_custom["environmentVariables"] = {
            "VSA_JWT_VERIFY_MODE": "enforce",
            "DEMO_ONLY_VAR": "demo-value",
        }
        req = _mod.build_update_artifact_request(
            _DEMO_ID, demo_custom, "v-new", staging_bucket=_BUCKET
        )
        assert req["environmentVariables"]["DEMO_ONLY_VAR"] == "demo-value"

    def test_keeps_demo_lifecycle(self):
        demo_custom = dict(_DEMO_RUNTIME)
        demo_custom["lifecycleConfiguration"] = {
            "idleRuntimeSessionTimeout": 1800,
            "maxLifetime": 14400,
        }
        req = _mod.build_update_artifact_request(
            _DEMO_ID, demo_custom, "v-new", staging_bucket=_BUCKET
        )
        assert req["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 1800

    def test_new_version_id_applied(self):
        req = _mod.build_update_artifact_request(
            _DEMO_ID, _DEMO_RUNTIME, "v-pushed", staging_bucket=_BUCKET
        )
        s3 = req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["versionId"] == "v-pushed"

    def test_demo_prefix_unchanged(self):
        req = _mod.build_update_artifact_request(
            _DEMO_ID, _DEMO_RUNTIME, "v-pushed", staging_bucket=_BUCKET
        )
        s3 = req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        assert s3["prefix"] == _DEMO_KEY

    def test_no_agentRuntimeName_in_update(self):
        req = _mod.build_update_artifact_request(
            _DEMO_ID, _DEMO_RUNTIME, "v-new", staging_bucket=_BUCKET
        )
        assert "agentRuntimeName" not in req
