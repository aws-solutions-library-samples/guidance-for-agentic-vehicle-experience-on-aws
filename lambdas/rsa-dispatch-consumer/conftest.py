"""conftest.py for rsa-dispatch-consumer (Task 2.5).

Adds the Lambda root and lambdas root to sys.path so that both
`import handler` and `from shared.rsa_client import ...` resolve
correctly when collecting and running tests.
"""
import sys
from pathlib import Path

_LAMBDA_ROOT = Path(__file__).resolve().parent      # lambdas/rsa-dispatch-consumer
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent                  # lambdas/

for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
