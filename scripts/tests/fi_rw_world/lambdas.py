"""Lambda Invoke served by the real handler code, with the function's live environment.

Each function exists in moto (so ``Get/UpdateFunctionConfiguration`` are moto's), and
``Invoke`` runs the real handler with the environment moto holds *at invoke time*, so
an environment override the script applies is what the handler sees. Behaviour
reproduced from the Lambda docs and the live records:

- ``InvocationType=Event`` returns 202 and runs on the next world pump (the script's
  next sleep); a failed async invoke is retried twice by Lambda (1 and 2 minutes
  later), and each attempt leaves its own run record (live: three ``aborted`` records
  per failed async invoke, 2026-10-09 20:54-20:56Z).
- ``RequestResponse`` runs now; an exception gives ``StatusCode: 200`` with
  ``FunctionError: "Unhandled"`` and the error payload.
- every failed attempt adds to the function's ``Errors`` metric; the alarm model in
  ``alarm.py`` evaluates it.
"""
from __future__ import annotations

import io
import json
import os
import traceback
import zipfile
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from scripts.tests.fi_rw_world.apigw import LambdaContext
from scripts.tests.fi_rw_world.awsfake import AwsError, Router, streaming

HandlerFn = Callable[[dict[str, Any], Any], Any]


@contextmanager
def lambda_env(env: dict[str, str], function_name: str, region: str) -> Iterator[None]:
    """Apply a function's environment for one call; restore exactly what was there."""
    saved = dict(os.environ)
    try:
        os.environ.update(env)
        os.environ["AWS_REGION"] = region
        os.environ["AWS_DEFAULT_REGION"] = region
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = function_name
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _zip() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("world.py", "def handler(e, c):\n    raise RuntimeError('world-only')\n")
    return b.getvalue()


class FakeLambda:
    def __init__(self, world: Any) -> None:
        self.w = world
        self.handlers: dict[str, HandlerFn] = {}
        self.invocations: list[dict[str, Any]] = []
        self.errors: list[tuple[str, float]] = []   # (function, epoch) per failed attempt
        self._queue: list[tuple[float, str, dict[str, Any], int]] = []
        self.before_run: list[Callable[[str, dict[str, Any]], None]] = []
        self.after_run: list[Callable[[str, dict[str, Any], Any], None]] = []
        self.start_async_inside_invoke = False

    def create(self, lam: Any, role_arn: str, name: str, handler_ref: str,
               env: dict[str, str], fn: HandlerFn, timeout: int = 30) -> None:
        lam.create_function(FunctionName=name, Runtime="python3.12", Role=role_arn,
                            Handler=handler_ref, Code={"ZipFile": _zip()}, Timeout=timeout,
                            Environment={"Variables": env})
        self.handlers[name] = fn

    def _env(self, name: str) -> dict[str, str]:
        cfg = self.w.internal("lambda").get_function_configuration(FunctionName=name)
        return dict((cfg.get("Environment") or {}).get("Variables") or {})

    def run(self, name: str, event: dict[str, Any], attempt: int = 1) -> tuple[bool, Any]:
        fn = self.handlers[name]
        rec: dict[str, Any] = {"function": name, "event": event, "attempt": attempt,
                               "at": self.w.clock.iso()}
        self.invocations.append(rec)
        for hook in self.before_run:
            hook(name, event)
        try:
            with lambda_env(self._env(name), name, self.w.region):
                result = fn(event, LambdaContext(name, remaining_ms=900_000))
            rec["ok"] = True
            for hook in self.after_run:
                hook(name, event, result)
            return True, result
        except Exception as exc:  # noqa: BLE001 — the function failed
            rec["ok"] = False
            rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["traceback"] = traceback.format_exc()
            self.errors.append((name, self.w.clock.epoch()))
            for hook in self.after_run:
                hook(name, event, exc)
            return False, exc

    def invoke(self, p: dict[str, Any]) -> dict[str, Any]:
        name = str(p["FunctionName"]).split(":function:")[-1].split(":")[0]
        if name not in self.handlers:
            raise AwsError("ResourceNotFoundException",
                           f"Function not found: {p['FunctionName']}", status=404)
        payload = p.get("Payload") or b"{}"
        if hasattr(payload, "read"):
            payload = payload.read()
        event = json.loads(payload or b"{}")
        kind = p.get("InvocationType") or "RequestResponse"
        if kind == "Event":
            if self.start_async_inside_invoke:
                # The run starts (and stamps started_at) before the invoke call returns,
                # and the call returns in the next second: the race a warm Lambda allows.
                self.run(name, event)
                self.w.clock.advance(2)
                return {"StatusCode": 202, "Payload": streaming(b"")}
            self._queue.append((self.w.clock.epoch(), name, event, 1))
            return {"StatusCode": 202, "Payload": streaming(b"")}
        if kind == "DryRun":
            return {"StatusCode": 204, "Payload": streaming(b"")}
        ok, result = self.run(name, event)
        self.w.clock.advance(1)   # ensure next invoke gets a distinct started_at
        if ok:
            return {"StatusCode": 200, "ExecutedVersion": "$LATEST",
                    "Payload": streaming(json.dumps(result, default=str).encode())}
        err = {"errorMessage": str(result), "errorType": type(result).__name__,
               "stackTrace": []}
        return {"StatusCode": 200, "FunctionError": "Unhandled", "ExecutedVersion": "$LATEST",
                "Payload": streaming(json.dumps(err).encode())}

    def pump(self) -> None:
        """Run queued async invokes that are due; schedule Lambda's two retries on failure."""
        now = self.w.clock.epoch()
        due = [q for q in self._queue if q[0] <= now]
        self._queue = [q for q in self._queue if q[0] > now]
        for _, name, event, attempt in due:
            ok, _ = self.run(name, event, attempt)
            if not ok and attempt < 3:
                self._queue.append((self.w.clock.epoch() + 60 * attempt, name, event, attempt + 1))

    def drain(self) -> None:
        while self._queue:
            self.w.clock.advance(60)

    def register(self, router: Router) -> None:
        router.add("lambda", "Invoke", self.invoke)
