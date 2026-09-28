"""conftest.py — lambdas/ root.

Under ``--import-mode=importlib`` (set globally in ``pytest.ini``) Python's
``sys.modules`` cache is shared across all test files, but each test module is
loaded with its own namespace.  Several lambdas define a top-level ``handler``
module (``lambdas/<name>/handler.py``).  When tests from multiple lambdas run in
the same session, the first ``handler`` loaded sits in ``sys.modules["handler"]``
and every subsequent ``import handler as h`` in a different lambda's test gets
the *wrong* handler.

Each test file that does ``import handler as h`` already inserts its own lambda
root into ``sys.path`` at module-load time (the standard per-lambda pattern).
The problem is purely the stale ``sys.modules`` entry.  The fix is two-fold,
applied before every test item in this tree:

1. Evict ``"handler"`` from ``sys.modules`` so the next ``import`` re-resolves.
2. Promote the current test item's lambda root to ``sys.path[0]`` so the
   re-import finds the *correct* ``handler.py`` even if another lambda's root
   was prepended by a previous test file.
"""

import sys
from pathlib import Path


def pytest_runtest_setup(item) -> None:
    """Isolate ``handler`` imports across lambda test suites."""
    # 1. Evict the cached 'handler' module.
    sys.modules.pop("handler", None)

    # 2. Promote this test's lambda root to sys.path[0].
    #    item.path is a pathlib.Path to the test file.
    test_path = Path(str(item.fspath))
    # Walk up until we find a 'tests' directory ancestor; the sibling is the lambda root.
    for parent in test_path.parents:
        if parent.name == "tests":
            lambda_root = str(parent.parent)
            if lambda_root in sys.path:
                sys.path.remove(lambda_root)
            sys.path.insert(0, lambda_root)
            break



# ── Membership-claim trust environment (Fix Group 29) ─────────────────────────
#
# `avx_scope.py` now reads `custom:fleetIds` / `custom:dealerIds` /
# `custom:districtIds` only when the matching `AVX_*_CLAIM_TRUSTED` env var is exactly
# "true". Those attributes are self-writable under an app client's default
# `WriteAttributes`, and review cycle 3 measured three routes returning 200 with
# another organisation's Findings on a forged claim. See
# `issues/2026-09-23-avx-membership-claims-ungated-on-read-paths/`.
#
# The pre-existing tests across these packages exercise ROUTING and SCOPE behaviour,
# not claim trust, so they run with the claims trusted — the environment those
# behaviours are designed for. Inverting them would have traded route coverage for
# gate coverage.
#
# The gates are covered explicitly by the `untrusted_claims`-marked tests, and by
# mutations `M-FG29-1..8`, which delete each gate and require a failure. That split is
# deliberate: a blanket "everything trusted" fixture with no dedicated untrusted tests
# would make the gates invisible to the suite — the defect shape (a guard verified
# where it runs, not where it is bypassed) that three consecutive review cycles found
# in this lineage.
#
# WHY THIS LIVES HERE rather than in each package's `tests/conftest.py`: the lambda
# directories contain hyphens (`api-avx-findings`), so they cannot be Python packages,
# and a `conftest.py` under each `tests/` dir resolves to the SAME module name
# `tests.conftest` — pytest rejects the second with "Plugin already registered under a
# different name". That is the same root cause as
# `issues/2026-09-23-pytest-test-handler-module-collision/`, hit while writing these
# fixtures. This file's path is unique, so it is the one correct home.

import pytest

_MEMBERSHIP_TRUST_VARS = (
    "AVX_FLEET_IDS_CLAIM_TRUSTED",
    "AVX_DEALER_IDS_CLAIM_TRUSTED",
    "AVX_DISTRICT_IDS_CLAIM_TRUSTED",
)
# Deliberately literal strings, not imported from `_lib.avx_scope`: this conftest is
# collected before any lambda root is on `sys.path`, and adding a path insertion here
# would fight `pytest_runtest_setup` above. `test_trust_var_names_match_the_source`
# in `lambdas/_lib/tests/test_avx_scope.py` pins these against the module constants, so
# a rename cannot silently desync them.


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "untrusted_claims: run with every membership-claim trust flag CLEARED "
        "(the deployed default), to assert fail-closed behaviour",
    )


@pytest.fixture(autouse=True)
def _trust_membership_claims(request, monkeypatch):
    """Trust the membership claims unless the test opts out.

    Opt out with `@pytest.mark.untrusted_claims` to assert the gates deny.
    """
    if request.node.get_closest_marker("untrusted_claims"):
        for var in _MEMBERSHIP_TRUST_VARS:
            monkeypatch.delenv(var, raising=False)
        return
    for var in _MEMBERSHIP_TRUST_VARS:
        monkeypatch.setenv(var, "true")
