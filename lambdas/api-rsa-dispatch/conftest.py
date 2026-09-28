"""conftest.py for api-rsa-dispatch.

Adds the Lambda root to sys.path so that `import handler` resolves when
pytest collects tests in `tests/`. The module is loaded under the unique
name `rsa_dispatch_handler` (via spec_from_file_location in test_handler.py)
to avoid the sys.modules["handler"] collision described in tasks.md § "Before
you start" and fixed in F4.10/F5.6.
"""
import sys
from pathlib import Path

_LAMBDA_ROOT = Path(__file__).resolve().parent   # lambdas/api-rsa-dispatch

if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))
