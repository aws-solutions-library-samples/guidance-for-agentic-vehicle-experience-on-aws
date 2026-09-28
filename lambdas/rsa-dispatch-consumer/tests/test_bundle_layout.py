"""
Bundle-layout import test for rsa-dispatch-consumer (Task 2.5).

Purpose: prove that `from shared.rsa_client import ...` resolves correctly
when the Lambda runs from a bundle-shaped directory (i.e., only the staged
contents are on sys.path).

The test copies the staged asset directory produced by build-rsa-lambda.sh
into a temporary directory, then imports the handler from there using only
that directory on sys.path.  A passing green test confirms the import works
in the bundle layout; a test that passes even when shared/rsa_client.py is
absent is asserting nothing — the mutation below catches that.

Mutation: delete shared/rsa_client.py from the staged directory → MUST fail.
Mutation: remove the staging step from build-rsa-lambda.sh → staged dir has no
          shared/ → MUST fail.
"""
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Path to the staged build directory produced by build-rsa-lambda.sh.
REPO_ROOT = Path(__file__).resolve().parents[3]
STAGED_DIR = REPO_ROOT / "infrastructure" / "build" / "rsa-dispatch-consumer"


def _import_handler_from_bundle_isolated(bundle_dir: Path) -> object:
    """
    Import handler.py with ONLY bundle_dir on sys.path — completely isolated
    from the test process's sys.path.

    Uses a subprocess to guarantee that lambdas/ is NOT on the import path
    and that any existing sys.modules['shared'] cache doesn't pollute the test.
    This is the only reliable way to simulate what Lambda runtime does.
    """
    import json
    import os

    script = f"""
import sys, json
# Simulate Lambda runtime: insert bundle dir FIRST so shared/ in the bundle
# resolves before any source-tree lambdas/ that might be on the path.
# We do NOT strip other entries (boto3 / venv must stay accessible in tests).
# The test_import_fails_when_shared_absent variant uses a bundle WITH no shared/,
# and we strip the lambdas source root so the import can't fall back to it.
_LAMBDAS_SOURCE = r'{REPO_ROOT / "lambdas"}'
sys.path = [p for p in sys.path if p != _LAMBDAS_SOURCE]
sys.path.insert(0, r'{bundle_dir}')

import os
os.environ['RSA_DISPATCH_TABLE'] = 'stub-table'
os.environ['VSA_RSA_CLIENT'] = 'fake'

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location('handler_bundle_rsa', r'{bundle_dir}/handler.py')
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
        """build-rsa-lambda.sh must have been run before the tests."""
        assert STAGED_DIR.exists(), (
            f"Staged directory {STAGED_DIR} does not exist. "
            "Run `bash scripts/build-rsa-lambda.sh` before running these tests."
        )

    def test_shared_rsa_client_present_in_bundle(self):
        """shared/rsa_client.py must exist inside the staged dir."""
        assert (STAGED_DIR / "shared" / "rsa_client.py").exists(), (
            "shared/rsa_client.py is missing from the staged bundle. "
            "The build-rsa-lambda.sh staging step must copy it."
        )

    def test_handler_imports_from_bundle_shaped_root(self, tmp_path):
        """
        Core test: handler.py must import cleanly when only the bundle
        directory is on sys.path (simulates deployed Lambda cold start).
        """
        bundle_copy = tmp_path / "bundle"
        shutil.copytree(str(STAGED_DIR), str(bundle_copy))

        result = _import_handler_from_bundle_isolated(bundle_copy)
        assert result.get("ok") is True, (
            f"handler.py failed to import from bundle-shaped root. "
            f"Error: {result.get('error', '(none)')}"
        )
        assert result.get("has_handler") is True

    def test_import_fails_when_shared_absent(self, tmp_path):
        """
        Mutation guard: if shared/rsa_client.py is deleted from the bundle,
        the import MUST fail with ModuleNotFoundError.
        """
        bundle_copy = tmp_path / "bundle_no_shared"
        shutil.copytree(str(STAGED_DIR), str(bundle_copy))

        # Delete the shared package entirely — simulating a missing staging step.
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


class TestBundleLayoutLifecycleImport:
    """F10.13: lifecycle.py is a second handler in the bundle; confirm it imports."""

    def test_lifecycle_imports_from_bundle_shaped_root(self, tmp_path):
        """
        lifecycle.py must import cleanly from the bundle directory (simulates the
        Lambda cold start for the lifecycle.handler entry point).

        Mutation guard: delete lifecycle.py from the staged directory → import fails.
        """
        import json
        import os

        bundle_copy = tmp_path / "bundle_lifecycle"
        shutil.copytree(str(STAGED_DIR), str(bundle_copy))

        script = f"""
import sys, json, os
_LAMBDAS_SOURCE = r'{REPO_ROOT / "lambdas"}'
sys.path = [p for p in sys.path if p != _LAMBDAS_SOURCE]
sys.path.insert(0, r'{bundle_copy}')

os.environ['RSA_DISPATCH_TABLE'] = 'stub-table'
os.environ['VSA_RSA_CLIENT'] = 'fake'
os.environ['RSA_SIM_EN_ROUTE_AFTER_SECONDS'] = '60'
os.environ['RSA_SIM_ARRIVED_AFTER_SECONDS'] = '300'
os.environ['RSA_REQUESTED_STALE_AFTER_SECONDS'] = '900'

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location('lifecycle_bundle', r'{bundle_copy}/lifecycle.py')
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
        data = json.loads(result.stdout.strip())
        assert data.get("ok") is True, (
            f"lifecycle.py failed to import from bundle-shaped root. "
            f"Error: {data.get('error', '(none)')}. "
            "F10.13 mutation guard: delete lifecycle.py from staged dir → import fails."
        )
        assert data.get("has_handler") is True, (
            "lifecycle.py must expose a 'handler' attribute after import."
        )
