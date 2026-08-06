"""conftest.py — shared sys.path wiring for test/integration/* suites.

Adds the CVX repo root to sys.path so that ``lambdas.*``, ``agents.*``
and sibling packages are importable without installing anything.

This file lives at ``test/conftest.py`` and is automatically loaded by
pytest for every test under ``test/``.
"""
import sys
import os

# CVX repo root — two levels above this conftest (test/conftest.py → test/ → /)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
