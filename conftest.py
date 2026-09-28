"""conftest.py — repo root.

Under ``--import-mode=importlib`` (set globally in ``pytest.ini`` as of the
2026-09-18 Group 1 tripwire wire-up) Python's ``sys.modules`` cache is shared
across all test files, but each test module is loaded with its own namespace.

Multiple lambdas under ``lambdas/<name>/`` define a top-level ``handler``
module. So do a handful of tests OUTSIDE ``lambdas/`` — most notably
``agents/supervisor/tests/test_fleet_manager_persona.py`` — which prepend a
specific lambda root to ``sys.path`` and then ``import handler as h``.

When a lambdas-tree test runs first, it seats *its* ``handler`` in
``sys.modules["handler"]``. The subsequent agents-tree test then does
``sys.path.insert(0, <different lambda root>)`` and ``import handler`` — and
gets the *previous* handler from the cache, not a fresh import from the newly
prepended path. The failure mode: an ``AttributeError`` naming a symbol that
lives on the intended handler but not on the cached one.

The ``lambdas/conftest.py`` fix for this was written in Fix Group 4 and works
correctly for tests *inside* ``lambdas/`` (its ``pytest_runtest_setup`` hook
fires only on collection items under that directory). Review Cycle 2 W2
identified the residue: agents-tree tests that import a lambda ``handler`` are
not covered by that conftest and hit the exact bug it was written to prevent.
Reproduced: ``test_fleet_manager_persona_requestable`` passes in isolation,
fails order-dependent in the full suite.

This repo-root hook covers them. It does ONLY the eviction — no ``sys.path``
manipulation, because tests outside ``lambdas/`` manage their own path (see
``agents/supervisor/tests/test_fleet_manager_persona.py:16-21``). The eviction
is idempotent, so ``lambdas/conftest.py`` re-evicting on lambdas-tree tests is
a harmless no-op.
"""

from __future__ import annotations

import sys


def pytest_runtest_setup(item) -> None:
    """Evict a possibly-stale ``handler`` module before every test item.

    Fixes cross-tree ``sys.modules['handler']`` pollution — see module docstring.
    Idempotent with the sibling hook at ``lambdas/conftest.py``.
    """
    sys.modules.pop("handler", None)
