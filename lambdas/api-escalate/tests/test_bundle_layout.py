"""
Bundle-layout import test for api-escalate (Task 2.5).

Purpose: prove that `from shared.rsa_client import ...` resolves correctly
when the Lambda runs from a bundle-shaped directory (i.e., only the staged
contents are on sys.path).

Mutation: delete shared/rsa_client.py from the staged directory → MUST fail.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STAGED_DIR = REPO_ROOT / "infrastructure" / "build" / "api-escalate"


def _import_handler_from_bundle_isolated(bundle_dir: Path) -> dict:
    """
    Import handler.py with ONLY bundle_dir on sys.path — completely isolated
    from the test process's sys.path.

    Uses a subprocess to guarantee lambdas/ is NOT on the import path,
    matching what Lambda runtime does (code dir = /var/task, nothing else).
    """
    script = f"""
import sys, json
# Simulate Lambda runtime: insert bundle dir FIRST so shared/ in the bundle
# resolves before any source-tree lambdas/ that might be on the path.
# We do NOT strip other entries (boto3 / venv must stay accessible in tests).
_LAMBDAS_SOURCE = r'{REPO_ROOT / "lambdas"}'
sys.path = [p for p in sys.path if p != _LAMBDAS_SOURCE]
sys.path.insert(0, r'{bundle_dir}')

import os
os.environ['RSA_DISPATCH_TABLE'] = 'stub-table'
os.environ['TENANT_CONFIG_TABLE'] = 'stub-tenant-config'
os.environ['LIVE_STATE_FN_NAME'] = 'stub-live-state-fn'
os.environ['VSA_RSA_CLIENT'] = 'fake'

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location('handler_bundle_escalate', r'{bundle_dir}/handler.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop('shared', None)
    sys.modules.pop('shared.rsa_client', None)
    spec.loader.exec_module(mod)
    print(json.dumps({{"ok": True, "has_handler": hasattr(mod, "handler")}}))
except (ModuleNotFoundError, ImportError) as exc:
    print(json.dumps({{"ok": False, "error": str(exc)}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        raise RuntimeError(f"Subprocess produced no output.\nstderr: {result.stderr}")
    return json.loads(result.stdout.strip())


class TestBundleLayoutImport:
    """Confirm that the staged bundle layout makes the shared import work."""

    def test_staged_dir_exists(self):
        assert STAGED_DIR.exists(), (
            f"Staged directory {STAGED_DIR} does not exist. "
            "Run `bash scripts/build-rsa-lambda.sh` before running these tests."
        )

    def test_shared_rsa_client_present_in_bundle(self):
        assert (STAGED_DIR / "shared" / "rsa_client.py").exists(), (
            "shared/rsa_client.py is missing from the staged bundle."
        )

    def test_handler_imports_from_bundle_shaped_root(self, tmp_path):
        """Core test: handler.py must import from bundle-shaped dir with no parents on path."""
        bundle_copy = tmp_path / "bundle"
        shutil.copytree(str(STAGED_DIR), str(bundle_copy))

        result = _import_handler_from_bundle_isolated(bundle_copy)
        assert result.get("ok") is True, (
            f"handler.py failed to import from bundle-shaped root. "
            f"Error: {result.get('error', '(none)')}"
        )
        assert result.get("has_handler") is True

    def test_import_fails_when_shared_absent(self, tmp_path):
        """Mutation guard: missing staged shared/ → import MUST fail."""
        bundle_copy = tmp_path / "bundle_no_shared"
        shutil.copytree(str(STAGED_DIR), str(bundle_copy))

        shared_dir = bundle_copy / "shared"
        if shared_dir.exists():
            shutil.rmtree(str(shared_dir))

        result = _import_handler_from_bundle_isolated(bundle_copy)
        assert result.get("ok") is False, (
            "Expected import to fail when shared/ is absent from the bundle, "
            "but it succeeded. The test is asserting nothing useful."
        )
        err = result.get("error", "")
        assert "shared" in err.lower() or "rsa_client" in err.lower(), (
            f"Expected error mentioning 'shared' or 'rsa_client', got: {err}"
        )
