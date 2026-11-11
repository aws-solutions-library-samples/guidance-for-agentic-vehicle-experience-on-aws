"""Fail loudly when the async test plugin is missing.

`pytest.ini` sets `asyncio_mode = auto`, which only means something when
pytest-asyncio is installed. Without it the option is "unknown" (a warning), and
pytest 8.3.3 SKIPS every `async def` test and exits 0. That is how 24 tests in
test_bidi_app.py and test_injection_queue.py, including every auth-rejection test
on the bidi voice boundary, went unexecuted until 2026-10-01
(issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).

These tests are synchronous on purpose. An async guard would be skipped by the
same mechanism it exists to catch.

Install: pip install -r requirements.txt -r agents/supervisor/requirements-dev.txt
"""

from __future__ import annotations


def test_pytest_asyncio_plugin_is_loaded(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("asyncio"), (
        "pytest-asyncio is not installed, so every async test in this suite is "
        "skipped (pytest 8) or failed (pytest 9). Install "
        "agents/supervisor/requirements-dev.txt."
    )


def test_asyncio_mode_is_auto(pytestconfig):
    # getini raises ValueError for an option no plugin registered, which is the
    # state pytest.ini is in when pytest-asyncio is absent.
    assert pytestconfig.getini("asyncio_mode") == "auto", (
        "test_injection_queue.py has unmarked async tests; they only run in "
        "asyncio_mode=auto (pytest.ini)."
    )
