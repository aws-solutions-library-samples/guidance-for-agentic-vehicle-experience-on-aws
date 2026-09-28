"""`.gitlab-ci.yml` must parse, and every script entry must be a string.

Why this exists. On 2026-09-28 the `publish_to_github` job shipped into tag
`v0.3.0` with this line:

    - echo "Token probe: GITHUB_TOKEN can reach the mirror."

The scalar is unquoted — the `"` is inside it, not at the start — so the `": "`
made YAML read the entry as a mapping:

    {'echo "Token probe': 'GITHUB_TOKEN can reach the mirror."'}

GitLab refused to create the pipeline at all:

    jobs:publish_to_github:script config should be a string or a nested array
    of strings up to 10 levels deep

Nothing caught it before the tag, because a tag pipeline is built from the
`.gitlab-ci.yml` *at the tagged commit*, and the only way to learn it was broken
was to push a protected tag and watch the pipeline fail to start. Recovering
meant deleting the protected tag through the web UI and re-cutting — the third
such round that day.

The trap is narrow and easy to re-enter: `echo "Audit log:"` in the same job is
fine, because its colon is not followed by a space. So the difference between a
working line and a pipeline that cannot start is one space, in a file no test
read until now.

Run: python3 -m pytest scripts/tests/test_gitlab_ci_wellformed.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".gitlab-ci.yml"

_SCRIPT_KEYS = ("script", "before_script", "after_script")


def _load() -> dict:
    assert CI.is_file(), f"{CI} is missing"
    doc = yaml.safe_load(CI.read_text())
    assert isinstance(doc, dict), f".gitlab-ci.yml parsed as {type(doc).__name__}, not a mapping"
    return doc


def _jobs(doc: dict) -> list[tuple[str, dict]]:
    """Job-shaped top-level entries: a mapping carrying at least one script key."""
    return [
        (name, body)
        for name, body in doc.items()
        if isinstance(body, dict) and any(k in body for k in _SCRIPT_KEYS)
    ]


def test_gitlab_ci_parses():
    _load()


def test_at_least_one_job_is_found():
    """Anti-vacuity: the checks below are meaningless if nothing is discovered."""
    jobs = _jobs(_load())
    assert len(jobs) >= 1, "found no jobs with a script key — the walker is not seeing the file"


def test_publish_job_is_present():
    """The job this test was written for. Its absence should fail loudly, not silently."""
    assert "publish_to_github" in _load(), (
        "publish_to_github is gone from .gitlab-ci.yml — if that is deliberate, "
        "delete this assertion; if not, the public-mirror publish path is unwired."
    )


def test_every_script_entry_is_a_string():
    """The actual defect. A dict here is the 'should be a string' pipeline error."""
    offenders: list[str] = []
    for name, body in _jobs(_load()):
        for key in _SCRIPT_KEYS:
            entries = body.get(key)
            if entries is None:
                continue
            if isinstance(entries, str):
                continue
            assert isinstance(entries, list), f"{name}.{key} is {type(entries).__name__}"
            for i, entry in enumerate(entries):
                if not isinstance(entry, str):
                    offenders.append(
                        f"{name}.{key}[{i}] parsed as {type(entry).__name__}: {entry!r}\n"
                        f"    Most likely an unquoted scalar containing ': '. "
                        f"Wrap the whole entry in single quotes."
                    )
    assert not offenders, "non-string script entries:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize(
    "line, should_parse_as_string",
    [
        ('echo "Token probe: GITHUB_TOKEN can reach the mirror."', False),
        ("'echo \"Token probe: GITHUB_TOKEN can reach the mirror.\"'", True),
        ('echo "Audit log:"', True),
        ("echo no colon here", True),
    ],
)
def test_the_walker_actually_distinguishes_the_trap(line, should_parse_as_string):
    """Positive control: prove the check would catch the real defect and pass the fix.

    Without this, `test_every_script_entry_is_a_string` could be green because it
    walks nothing, or because YAML is more forgiving than believed.
    """
    parsed = yaml.safe_load(f"script:\n    - {line}\n")["script"][0]
    assert isinstance(parsed, str) is should_parse_as_string, (
        f"{line!r} parsed as {type(parsed).__name__}; expected "
        f"{'a string' if should_parse_as_string else 'a non-string (the trap)'}"
    )
