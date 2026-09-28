"""conftest.py for api-escalate (Task 2.5).

Adds the Lambda root and lambdas root to sys.path so that both
`import handler` and `from shared.rsa_client import ...` resolve
correctly when collecting and running tests.

The staged asset pattern (scripts/build-rsa-lambda.sh) puts shared/ inside
the bundle directory alongside handler.py, so in a deployed Lambda the import
resolves via Lambda's default /var/task on sys.path. In tests, the source tree
needs lambdas/ on sys.path for the same import to work from source.

This conftest runs at collection time, before test_handler.py's module-level
path setup code, so `import handler as h` in test_handler.py finds the
correct lambdas/ root.
"""
import sys
from pathlib import Path

_LAMBDA_ROOT = Path(__file__).resolve().parent      # lambdas/api-escalate
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent                  # lambdas/

for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
