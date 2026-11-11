"""``fi_loop_world.cms_fi.ensure_path`` must not leave its CMS copy in the temp folder.

``ensure_path`` extracts CMS at ``CMS_COMMIT`` (``services/`` + ``deployment/``, ~42 MB
with the tar) into ``cms_fi_<commit>_*`` under the temp folder, once per process.
Nothing deleted it, so every pytest process that touched the loop world (each mutant
of a battery is one) left a copy behind. They reached 35 GB and filled the disk on
2026-10-24 (issues/2026-10-24-cms-fi-temp-copies-fill-disk/).

Each test runs ``ensure_path`` in a child Python with its own ``TMPDIR``, lets the
child exit, and then checks the child's temp folder. A test in this process could
not see the property: the copy is removed when the process exits. The cases are a
normal run, a git-archive failure, an extraction failure (ENOSPC, the disk-full case)
and two extractions in one process (the W12 test resets the cache).

The child records every ``tempfile.mkdtemp`` call, so each test also proves the
writer really made a copy in that process; an empty temp folder alone would pass
if the copy were made somewhere else.

A killed child (SIGKILL, SIGTERM) still leaves its copy; that is not covered here.

Run:
    .venv/bin/python -m pytest scripts/tests/test_fi_loop_world_cms_fi_cleanup.py -v

Requires ~/connected-mobility-guidance-on-aws with commit ``CMS_COMMIT`` present
(env ``FI_LOOP_WORLD_CMS_REPO`` to override), like the other loop-world tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = r"""
import json, os, sys, tempfile

sys.path.insert(0, sys.argv[1])
made = []
_real_mkdtemp = tempfile.mkdtemp


def _recording_mkdtemp(*args, **kwargs):
    path = _real_mkdtemp(*args, **kwargs)
    made.append(path)
    return path


tempfile.mkdtemp = _recording_mkdtemp

from scripts.tests.fi_loop_world import cms_fi as cf

report = {"made": made}
mode = sys.argv[2]
if mode == "ok":
    cf.ensure_path()
    root = cf.ensure_path._extracted_root
    report["root"] = root
    report["index_exists"] = os.path.isfile(
        os.path.join(root, "services", "fleet_intelligence", "index.py"))
    report["archive_exists"] = os.path.exists(os.path.join(root, "archive.tar"))
elif mode == "twice":
    # test_live_verify_fi_loop.py's W12 test resets the cache, so a full run of that
    # file extracts a second copy in the same process.
    cf.ensure_path()
    del cf.ensure_path._extracted_root
    cf.ensure_path()
elif mode == "extract-fail":
    import errno, tarfile

    def _no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    tarfile.open = _no_space
    try:
        cf.ensure_path()
    except OSError as exc:
        report["error"] = str(exc)
else:
    try:
        cf.ensure_path()
    except RuntimeError as exc:
        report["error"] = str(exc)
print("REPORT " + json.dumps(report))
"""


def _run_child(mode: str, child_tmp: Path, extra_env: dict[str, str]) -> dict[str, Any]:
    child_tmp.mkdir()
    env = dict(os.environ)
    env.update(extra_env)
    env["TMPDIR"] = str(child_tmp)
    # -X dev and -W always show ResourceWarning (even if PYTHONWARNINGS=ignore is
    # inherited), so an archive file handle left open is visible.
    proc = subprocess.run(
        [sys.executable, "-X", "dev", "-W", "always::ResourceWarning",
         "-c", _CHILD, str(_REPO_ROOT), mode],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, (
        f"child exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    assert len(lines) == 1, f"no REPORT line\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    report = json.loads(lines[0][len("REPORT "):])
    unclosed = [
        ln for ln in proc.stderr.splitlines()
        if "ResourceWarning" in ln and "unclosed file" in ln and "cms_fi_" in ln]
    assert unclosed == [], f"the archive file handle was left open: {unclosed}"
    return report


def _cms_copies_made(report: dict[str, Any]) -> list[str]:
    return [p for p in report["made"] if os.path.basename(p).startswith("cms_fi_")]


def test_extracted_cms_copy_is_removed_at_process_exit(tmp_path: Path) -> None:
    child_tmp = tmp_path / "child-tmp"
    report = _run_child("ok", child_tmp, {})

    made = _cms_copies_made(report)
    assert len(made) == 1, f"expected the child to make one cms_fi_ copy, made {report['made']}"
    root = Path(report["root"])
    assert str(root) == made[0]
    assert root.parent.resolve() == child_tmp.resolve(), (
        f"copy was made outside the child's TMPDIR: {root}")
    assert report["index_exists"], "the copy never held the extracted CMS tree"
    assert not report["archive_exists"], (
        "archive.tar was kept after extraction; it doubles the copy's size")

    assert not root.exists(), f"the child exited and left its CMS copy behind: {root}"
    assert sorted(child_tmp.glob("cms_fi_*")) == []


def test_every_copy_is_removed_when_the_cache_is_reset(tmp_path: Path) -> None:
    child_tmp = tmp_path / "child-tmp"
    report = _run_child("twice", child_tmp, {})

    made = _cms_copies_made(report)
    assert len(made) == 2, f"expected the child to make two cms_fi_ copies, made {report['made']}"
    left = [p for p in made if Path(p).exists()]
    assert left == [], f"the child exited and left copies behind: {left}"
    assert sorted(child_tmp.glob("cms_fi_*")) == []


def test_copy_is_removed_when_extraction_fails(tmp_path: Path) -> None:
    child_tmp = tmp_path / "child-tmp"
    report = _run_child("extract-fail", child_tmp, {})

    assert "No space left on device" in report.get("error", ""), (
        f"expected the patched extraction to fail, got {report}")
    made = _cms_copies_made(report)
    assert len(made) == 1, f"expected the child to make one cms_fi_ copy, made {report['made']}"
    assert Path(made[0]).parent.resolve() == child_tmp.resolve()

    assert not Path(made[0]).exists(), (
        f"the child exited after a failed extraction and left its copy behind: {made[0]}")
    assert sorted(child_tmp.glob("cms_fi_*")) == []


def test_copy_is_removed_when_git_archive_fails(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    child_tmp = tmp_path / "child-tmp"
    report = _run_child("fail", child_tmp, {
        "FI_LOOP_WORLD_CMS_REPO": str(not_a_repo),
        # Stop git from finding a repository above tmp_path.
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    })

    assert "git archive" in report.get("error", ""), (
        f"expected ensure_path to raise its git-archive RuntimeError, got {report}")
    made = _cms_copies_made(report)
    assert len(made) == 1, f"expected the child to make one cms_fi_ copy, made {report['made']}"
    assert Path(made[0]).parent.resolve() == child_tmp.resolve()

    assert not Path(made[0]).exists(), (
        f"the child exited after a failed archive and left its copy behind: {made[0]}")
    assert sorted(child_tmp.glob("cms_fi_*")) == []
