"""Short-circuit selected botocore operations with world handlers.

moto serves most services. Where moto cannot (Cognito needs ``joserfc``, which the
venv lacks; ``DescribeAlarmHistory`` is not implemented; Athena, Lambda Invoke,
Bedrock and AgentCore need the real code behind them), a ``before-call`` handler
returns the response instead. botocore then treats it exactly as a wire response:
a status >= 300 raises the modeled ``ClientError`` subclass for ``Error.Code``.

Handlers see the caller's API parameters (captured at ``before-parameter-build``, so
the wire protocol does not matter). They are registered on every client the process
creates while the world is installed (``botocore.session.Session.create_client`` is
wrapped), so clients built by the script, the real handlers and the real runner all
see them.
"""
from __future__ import annotations

import io
from typing import Any, Callable

import botocore.session
from botocore.response import StreamingBody

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class FakeHttp:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.content = b""
        self.raw = None


class AwsError(Exception):
    """Raise from a handler to return an AWS error response."""

    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


def streaming(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


class Router:
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], Handler] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []      # served by a handler
        self.all_calls: list[tuple[str, str, dict[str, Any]]] = []  # every AWS call
        self._orig_create_client: Any = None

    def add(self, service: str, operation: str, fn: Handler) -> None:
        self._handlers[(service, operation)] = fn

    def handles(self, service: str, operation: str) -> bool:
        return (service, operation) in self._handlers

    @staticmethod
    def _capture(params: dict[str, Any], context: dict[str, Any], **_: Any) -> None:
        context["fi_rw_api_params"] = dict(params)

    def _before_call(self, model: Any, params: dict[str, Any], context: dict[str, Any],
                     **_: Any) -> Any:
        if self is not _CURRENT.get("router"):
            return None
        key = (model.service_model.service_name, model.name)
        api = dict(context.get("fi_rw_api_params") or {})
        self.all_calls.append((key[0], key[1], api))
        fn = self._handlers.get(key)
        if fn is None:
            return None
        self.calls.append((key[0], key[1], api))
        try:
            parsed = fn(api)
            if parsed is None:  # the handler only observed the call: let moto serve it
                return None
            parsed.setdefault("ResponseMetadata", {"HTTPStatusCode": 200})
            return FakeHttp(200), parsed
        except AwsError as e:
            return FakeHttp(e.status), {
                "Error": {"Code": e.code, "Message": e.message},
                "ResponseMetadata": {"HTTPStatusCode": e.status},
            }

    def install(self) -> None:
        """Route every client the process creates to the current world's router.

        The wrapper is installed once per process and stays; it delegates to whichever
        router is current, so a client a module cached in an earlier world (DMS and AVX
        handlers cache their boto3 resources) reaches this world, never a stale one.
        """
        _CURRENT["router"] = self
        if _CURRENT.get("installed"):
            return
        orig = botocore.session.Session.create_client

        def create_client(sess: Any, *a: Any, **kw: Any) -> Any:
            client = orig(sess, *a, **kw)
            ev = client.meta.events
            ev.register_first("before-parameter-build.*.*", Router._capture,
                              unique_id="fi-rw-world-capture")
            ev.register_first("before-call.*.*", _dispatch, unique_id="fi-rw-world-router")
            return client

        botocore.session.Session.create_client = create_client  # type: ignore[method-assign]
        _CURRENT["installed"] = True

    def uninstall(self) -> None:
        if _CURRENT.get("router") is self:
            _CURRENT["router"] = None


_CURRENT: dict[str, Any] = {}


def _dispatch(**kw: Any) -> Any:
    router = _CURRENT.get("router")
    return None if router is None else router._before_call(**kw)
