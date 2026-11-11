"""scripts/lib/check-bundle-abi.py must refuse a bundle the Lambda cannot load.

Regression guard for issues/2026-10-01-ws-authorizer-bundle-built-for-python-3-14/:
the staging authorizer shipped `_cffi_backend.cpython-314-x86_64-linux-gnu.so` on a
python3.12 Lambda, and nothing in the build noticed.

Run: python3 -m pytest scripts/tests/test_check_bundle_abi.py -v
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "check-bundle-abi.py"
_spec = importlib.util.spec_from_file_location("check_bundle_abi", _SCRIPT)
abi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(abi)


def _elf(machine: int) -> bytes:
    """A minimal 64-bit little-endian ELF header with the given e_machine."""
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # 64-bit
    header[5] = 1  # little-endian
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


X86_64 = _elf(0x3E)
AARCH64 = _elf(0xB7)
MACHO = b"\xcf\xfa\xed\xfe" + bytes(60)


def _bundle(tmp_path: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return tmp_path


def test_a_cp312_linux_bundle_passes(tmp_path):
    b = _bundle(tmp_path, {
        "_cffi_backend.cpython-312-x86_64-linux-gnu.so": X86_64,
        "cryptography/hazmat/bindings/_rust.abi3.so": X86_64,
        "jwt/__init__.py": b"",
    })
    assert abi.problems(b) == []
    assert abi.main([str(b)]) == 0


def test_a_pure_python_bundle_passes(tmp_path):
    assert abi.main([str(_bundle(tmp_path, {"handler.py": b""}))]) == 0


def test_the_2026_09_17_authorizer_bundle_fails(tmp_path):
    b = _bundle(tmp_path, {"_cffi_backend.cpython-314-x86_64-linux-gnu.so": X86_64})
    found = abi.problems(b)
    assert len(found) == 1 and "CPython 314" in found[0]
    assert abi.main([str(b)]) == 1


@pytest.mark.parametrize("name, data, needle", [
    ("_yaml.cpython-312-darwin.so", MACHO, "not a Linux"),
    ("_rust.abi3.so", MACHO, "not a Linux"),
    ("_rust.abi3.so", AARCH64, "machine 0xb7"),
    ("md.cpython-313-x86_64-linux-gnu.so", X86_64, "CPython 313"),
])
def test_wrong_platform_or_version_fails(tmp_path, name, data, needle):
    found = abi.problems(_bundle(tmp_path, {f"pkg/{name}": data}))
    assert any(needle in f for f in found), found


def test_the_target_version_is_configurable(tmp_path):
    b = _bundle(tmp_path, {"x.cpython-313-x86_64-linux-gnu.so": X86_64})
    assert abi.problems(b, "3.13") == []
    assert abi.problems(b, "3.12") != []


def test_a_missing_directory_is_an_error(tmp_path):
    assert abi.main([str(tmp_path / "absent")]) == 2




# ---------- The build scripts actually run the check, and it gates the build ------
#
# Review 2026-10-01 (C2): the first version only looked for the string
# "check-bundle-abi.py", so `|| true` after the call, or commenting it out, stayed green.

_SCRIPTS = Path(__file__).resolve().parents[1]
_ALL_BUILDS = [
    "build-api-ws-authorizer.sh",
    "build-api-vehicle-live-state.sh",
    "build-avx-lambda.sh",
    "build-triage-classifier.sh",
]
_GATE = re.compile(
    r'^\s*python3 "\$ROOT/scripts/lib/check-bundle-abi\.py" "\$OUT" --python 3\.12 \\\n'
    r'\s*\|\| \{ echo "ERROR: removed \$OUT so it cannot be deployed" >&2; rm -rf "\$OUT"; exit 1; \}\s*$',
    re.MULTILINE,
)


@pytest.mark.parametrize("script", _ALL_BUILDS)
def test_every_python_build_targets_312_and_gates_on_the_check(script):
    text = (_SCRIPTS / script).read_text()
    assert "set -euo pipefail" in text, script
    pip_calls = text.split("pip install")[1:]
    assert pip_calls, f"{script}: no pip install found"
    for call in pip_calls:
        call = call.split(">/dev/null", 1)[0]
        assert "--python-version 3.12" in call, f"{script}: pip install without --python-version 3.12"
        assert "--implementation cp" in call, f"{script}: pip install without --implementation cp"
        assert "--platform manylinux2014_x86_64" in call, script
    gates = list(_GATE.finditer(text))
    assert len(gates) == 1, f"{script}: expected one gated check-bundle-abi.py call, found {len(gates)}"
    assert gates[0].start() > text.rindex("pip install"), f"{script}: the check runs before pip install"


_FAKE_PYTHON = r'''#!/usr/bin/env bash
# Stand-in for python3: `-m pip install ... --target DIR` drops one compiled file into DIR
# whose CPython tag is $FAKE_TAG; anything else runs the real interpreter.
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
  target=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--target" ]]; then target="$2"; fi
    shift
  done
  "$REAL_PYTHON" - "$target" <<'PY'
import sys
from pathlib import Path
import os
header = bytearray(64); header[:4] = b"\x7fELF"; header[4] = 2; header[5] = 1
header[18:20] = (0x3E).to_bytes(2, "little")
Path(sys.argv[1], f"_fake.cpython-{os.environ['FAKE_TAG']}-x86_64-linux-gnu.so").write_bytes(bytes(header))
PY
  exit 0
fi
exec "$REAL_PYTHON" "$@"
'''


def _fake_tree(tmp_path: Path, script: str, lambda_dir: str) -> tuple[Path, dict]:
    """A copy of the repo layout one build script needs, plus a fake python3 on PATH."""
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / script).write_text((_SCRIPTS / script).read_text())
    (root / "scripts" / "lib" / "check-bundle-abi.py").write_text(_SCRIPT.read_text())
    lam = root / "lambdas" / lambda_dir
    lam.mkdir(parents=True)
    (lam / "handler.py").write_text("def handler(event, context):\n    return {}\n")
    (lam / "requirements.txt").write_text("example==1.0\n")
    (root / "lambdas" / "shared").mkdir()
    (root / "lambdas" / "shared" / "__init__.py").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text(_FAKE_PYTHON)
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "REAL_PYTHON": sys.executable}
    return root, env


@pytest.mark.parametrize("script, lambda_dir", [
    ("build-api-ws-authorizer.sh", "api-ws-authorizer"),
    ("build-api-vehicle-live-state.sh", "api-vehicle-live-state"),
])
@pytest.mark.parametrize("tag, passes", [("312", True), ("314", False)])
def test_the_build_fails_and_removes_a_bundle_the_lambda_cannot_load(tmp_path, script, lambda_dir, tag, passes):
    """Run the real build script with a pip stand-in that installs a cp<tag> binary."""
    root, env = _fake_tree(tmp_path, script, lambda_dir)
    env["FAKE_TAG"] = tag
    run = subprocess.run(["bash", str(root / "scripts" / script)], env=env,
                         capture_output=True, text=True, timeout=60)
    out = root / "infrastructure" / "build" / lambda_dir
    if passes:
        assert run.returncode == 0, run.stderr
        assert (out / f"_fake.cpython-{tag}-x86_64-linux-gnu.so").is_file()
    else:
        assert run.returncode != 0, "a cp314 bundle built successfully"
        assert "CPython 314" in run.stderr, run.stderr
        assert not out.exists(), "the rejected bundle was left where cdk deploy would ship it"
