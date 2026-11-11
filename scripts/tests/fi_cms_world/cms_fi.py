"""CMS Fleet Intelligence Lambda behind a FakeRestApi.

CMS commit pinned to ``1ce93a75b``. Extracted via ``git archive`` into a per-process temp
dir; raises ``RuntimeError`` on failure (no working-tree fallback). Module re-imported
fresh per world instance so two CmsWorld instances never share handler module state.

FACTS (cms:services/fleet_intelligence/index.py:1ce93a75b):
  - handler() is the Lambda entry point (line 3284).
  - _authorize_or_403 guards every /api/v1/fleet-intelligence/* route (line 3374).
  - GET /pm/plans returns {"data": [plans]} via pm_plans._resp(200, ...) (pm_plans.py:65).
  - 403 body: {"error": "Not authorized for the requested fleet scope"} (line ~2935).
"""
from __future__ import annotations

import contextlib
import importlib
import io
import os
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

from scripts.tests.fi_rw_world.apigw import FakeRestApi, LambdaContext
from scripts.tests.fi_rw_world.lambdas import lambda_env

CMS_REPO = Path(os.environ.get("FI_CMS_REPO",
                               str(Path.home() / "connected-mobility-guidance-on-aws")))
CMS_COMMIT = "1ce93a75b"

# Stage D fleet routes + every maker/population route.
# maker._READ_ROUTES + _WRITE_ROUTES at CMS 1ce93a75b (maker.py:1171-1184).
_ROUTES = [
    ("/api/v1/fleet-intelligence/faults", "GET"),
    ("/api/v1/fleet-intelligence/faults/summary", "GET"),
    ("/api/v1/fleet-intelligence/vehicles/{vin}/faults", "GET"),
    ("/api/v1/fleet-intelligence/health-findings", "GET"),
    ("/api/v1/fleet-intelligence/pm/plans", "GET"),
    ("/api/v1/fleet-intelligence/pm/plans", "POST"),
    ("/api/v1/fleet-intelligence/pm/plans/{planId}", "PUT"),
    ("/api/v1/fleet-intelligence/pm/plans/{planId}/complete", "POST"),
    ("/api/v1/fleet-intelligence/pm/schedules", "GET"),
    ("/api/v1/fleet-intelligence/pm/compliance", "GET"),
    ("/api/v1/fleet-intelligence/groundings", "POST"),
    ("/api/v1/fleet-intelligence/groundings/{vehicleId}/return-to-service", "POST"),
    # population read routes
    ("/api/v1/fleet-intelligence/population/patterns", "GET"),
    ("/api/v1/fleet-intelligence/population/patterns/{patternId}", "GET"),
    ("/api/v1/fleet-intelligence/population/signals", "GET"),
    ("/api/v1/fleet-intelligence/population/health-rollup", "GET"),
    ("/api/v1/fleet-intelligence/population/threads", "GET"),
    ("/api/v1/fleet-intelligence/population/threads/{threadId}", "GET"),
    ("/api/v1/fleet-intelligence/population/campaigns/software", "GET"),
    ("/api/v1/fleet-intelligence/population/investigations/{investigationId}", "GET"),
    # population write routes
    ("/api/v1/fleet-intelligence/population/patterns/{patternId}/investigations", "POST"),
    ("/api/v1/fleet-intelligence/population/investigations/{investigationId}/decision", "POST"),
    ("/api/v1/fleet-intelligence/population/investigations/{investigationId}/decline", "POST"),
    ("/api/v1/fleet-intelligence/population/threads/{threadId}/issue", "POST"),
]


def extract_cms_archive(tmp_dir: str) -> Path:
    """Extract ``services/fleet_intelligence/`` from CMS_COMMIT into *tmp_dir*.

    Raises ``RuntimeError`` immediately on failure; never falls back to the working tree.
    """
    result = subprocess.run(
        ["git", "-C", str(CMS_REPO), "archive", "--format=tar",
         CMS_COMMIT, "services/fleet_intelligence/"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git archive {CMS_COMMIT} failed (rc={result.returncode}): "
            f"{result.stderr.decode()[:300]}"
        )
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as t:
        t.extractall(tmp_dir)
    return Path(tmp_dir)


def _reload_index(src_root: Path) -> Any:
    """Clear cached ``services.*`` modules, add *src_root* to path, import handler."""
    for name in list(sys.modules):
        if name == "services" or name.startswith("services."):
            del sys.modules[name]
    root_str = str(src_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return importlib.import_module("services.fleet_intelligence.index")


class CmsFiApi:
    """Real CMS FI handler served through a FakeRestApi (API Gateway REST proxy)."""

    def __init__(self, pool: Any, env: dict[str, str], src_root: Path,
                 *, avx_url: str = "") -> None:
        self.env = env
        self._fn_name = "cms-staging-ui-FleetIntelligenceFunction"
        self._avx_url = avx_url  # world's AVX fake API; calls to AVX_API_ENDPOINT host go here
        self._mod = _reload_index(src_root)
        self.api = FakeRestApi(
            name="cms-fi",
            stage="prod",
            verify_token=pool.verify,
            invoke=self._invoke,
        )
        for route, method in _ROUTES:
            self.api.add(route, method, route, self._mod.handler)

    def _invoke(self, _name: str, fn: Any, event: dict, ctx: LambdaContext) -> dict:
        with lambda_env(self.env, self._fn_name, "us-west-2"), \
             self._patch_avx_transport():
            return fn(event, ctx)

    @contextlib.contextmanager
    def _patch_avx_transport(self) -> "contextlib.AbstractContextManager[None]":
        """Route call_avx → world's AVX FakeRestApi at the transport level.

        maker.call_avx uses _OPENER.open (a module-level urllib.request.OpenerDirector
        built with _NoRedirect) to call AVX_API_ENDPOINT.  AVX_API_ENDPOINT is
        "https://avx-api.example.invalid/staging/" (scrubbed, starts with https so
        avx_base_url() accepts it).  We patch _OPENER.open on the loaded maker module
        so requests to avx-api.example.invalid are redirected to self._avx_url.

        Stage prefix: the url is base + path → ".../staging/investigations".  avx_url
        is "http://127.0.0.1:PORT/staging/" so we strip the stage prefix from the path.
        """
        if not self._avx_url:
            yield
            return
        import urllib.parse as _up  # noqa: PLC0415
        import sys  # noqa: PLC0415
        maker_mod = sys.modules.get("services.fleet_intelligence.maker")
        if maker_mod is None or not hasattr(maker_mod, "_OPENER"):
            yield
            return
        avx_ep = self.env.get("AVX_API_ENDPOINT", "")
        fake_host = _up.urlsplit(avx_ep).netloc            # avx-api.example.invalid
        avx_stage_prefix = _up.urlsplit(avx_ep).path.rstrip("/")  # /staging
        world_base = self._avx_url.rstrip("/")             # http://127.0.0.1:PORT/staging
        _orig_open = maker_mod._OPENER.open

        def _fake_open(req: Any, timeout: Any = None) -> Any:
            url = req.full_url if hasattr(req, "full_url") else req
            p = _up.urlsplit(url)
            if p.netloc != fake_host:
                return _orig_open(req, timeout=timeout)
            path = p.path
            if avx_stage_prefix and path.startswith(avx_stage_prefix):
                path = path[len(avx_stage_prefix):]
            new_url = world_base + path + (("?" + p.query) if p.query else "")
            new_req = urllib.request.Request(new_url, data=req.data, method=req.get_method())
            for h, v in req.headers.items():
                new_req.add_header(h, v)
            return urllib.request.urlopen(new_req, timeout=timeout)

        maker_mod._OPENER.open = _fake_open  # type: ignore[method-assign]
        try:
            yield
        finally:
            maker_mod._OPENER.open = _orig_open

    def close(self) -> None:
        self.api.close()
