"""A fake staging for ``live-verify-fi-rebalancing.py --part cms`` / ``cms-next``, built from the
real code (task 7.5).

Real code behind every request the script makes:

- **CMS** (loaded from CMS commit :data:`CMS_COMMIT` with ``git archive``, never from the
  sibling checkout's working tree): the FI Lambda ``services/fleet_intelligence/index.py``
  serves the rebalancing reads, ``POST /moves``, the groundings route and return to service;
  the main API's ``fi_action_queue.route`` serves ``POST /actions/{findingId}/approve`` (the
  screen's approve) and forwards the caller's token to AVX.
- **AVX** (this tree): ``lambdas/api-avx-findings/handler.py`` approves, records the decision
  Action and dispatches through the real executor registry, so ``CmsExecutor`` posts the move
  to CMS over HTTPS with the operator's token, as staging does.
- **Data**: the move Findings are written by the real rebalancing agent
  (``agents.tier2.runner.run_rebalance`` with the real ``ArtifactWriter``); CMS's rebalance
  views are published by the real ``refresh-rebalance-cache`` task (CMS handler, real
  ``rebalance_refresh`` and ``lifecycle_cache``); the recall views RTS reads are written with
  the real ``lifecycle_cache.publish``.

What is not real code, and why:

- the agent's ADP reads and CMS's five refresh queries are served from the fixture below
  (Athena returns every value as a string, two rows per page);
- Cognito is ``fi_rw_world.cognito.FakeUserPool`` (moto's needs ``joserfc``), with
  ``ListUserPools`` added;
- the two REST APIs are a local HTTPS server per API (self-signed certificate for 127.0.0.1,
  trusted through ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``) that verifies the pool's token
  and builds the REST proxy event, as ``fi_rw_world.apigw`` does;
- main_api's ``Caller`` (groups, ``custom:fleetIds``, unscoped, read-only) is derived here the
  way main_api's handler derives it (``index.py`` lines 1978-2041 at the commit);
- the recall view bodies (cohort, no campaigns) are fixture data.

Defect switches (off by default) put a real regression into the real route, so a test can
check the script reports it: :attr:`CmsWorld.defects`.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import http.server
import importlib.util
import io
import json
import os
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import traceback
import urllib.parse
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from scripts.tests.fi_rw_world.awsfake import AwsError, Router
from scripts.tests.fi_rw_world.cognito import FakeUserPool

REPO = Path(__file__).resolve().parents[2]
CMS_REPO = Path(os.environ.get("FI_REBAL_CMS_REPO",
                               str(Path.home() / "connected-mobility-guidance-on-aws")))
#: CMS main at 2026-10-23: same fleet-intelligence code as the deployed build (70a1d05c7) except
#: one unused ``adp_source`` constant (f6cc82dfd). The rebalancing fleets are the rental-typed
#: fleet records (FLEETS_TABLE_NAME), not REBALANCE_FLEET_IDS, which the deploy removed.
CMS_COMMIT = "75007152b"

STAGE, REGION, ADP_REGION, ACCOUNT = "staging", "us-west-2", "us-east-1", "123456789012"
RENTAL, DELIVERY = "demo-rental-carshare", "demo-lastmile-delivery"
POOL_NAME = f"cms-{STAGE}-ui-users"

VEHICLES = f"cms-{STAGE}-storage-vehicles"
FLEETS = f"cms-{STAGE}-storage-fleets"
#: Each fleet record's ``fleetType``, as staging's (get-item 2026-10-23). CMS's FI routes and its
#: refresh treat a fleet as rental when this is ``rental``, as depot when it is ``delivery``,
#: ``field``, or ``public``; anything else is ``neither``.
FLEET_TYPES = {RENTAL: "rental", DELIVERY: "delivery"}
GROUNDINGS = f"cms-{STAGE}-storage-vehicle-groundings"
MOVES = f"cms-{STAGE}-storage-vehicle-moves"
LOCS = f"cms-{STAGE}-storage-fleet-locations"
DRIVERS = f"cms-{STAGE}-storage-drivers"
LIVE_PLAN = f"cms-{STAGE}-storage-live-plan"
FINDINGS = f"vsa-{STAGE}-avx-findings"
ACTIONS = f"vsa-{STAGE}-avx-actions"
RUNS = f"vsa-{STAGE}-avx-agent-runs"
RULES = f"vsa-{STAGE}-avx-approval-rules"
CMS_RESULTS = f"cms-{STAGE}-athena-results-{ACCOUNT}-{ADP_REGION}"
POLICY_PARAM = f"/vsa/{STAGE}/avx/fi-rebalancing/policy"

# Two metros with spare cars (as staging's ATL and AUS, 2026-10-21). Downtown is the surplus
# in each; the airport, hub 1 and hub 2 are short. Parking is the destination-room check.
LOCATIONS = [
    {"locationId": "loc-atl-downtown", "name": "ATL Downtown", "metro": "atl",
     "locationType": "downtown", "latitude": "33.75", "longitude": "-84.39",
     "parkingSpaces": "40", "operatingHours": "6-22"},
    {"locationId": "loc-atl-airport", "name": "ATL Airport", "metro": "atl",
     "locationType": "airport", "latitude": "33.64", "longitude": "-84.43",
     "parkingSpaces": "30", "operatingHours": "0-24"},
    {"locationId": "loc-atl-hub1", "name": "ATL Hub 1", "metro": "atl",
     "locationType": "carshare_hub", "latitude": "33.78", "longitude": "-84.38",
     "parkingSpaces": "20", "operatingHours": "6-22"},
    {"locationId": "loc-aus-downtown", "name": "AUS Downtown", "metro": "aus",
     "locationType": "downtown", "latitude": "30.27", "longitude": "-97.74",
     "parkingSpaces": "40", "operatingHours": "6-22"},
    {"locationId": "loc-aus-hub2", "name": "AUS Hub 2", "metro": "aus",
     "locationType": "carshare_hub", "latitude": "30.30", "longitude": "-97.72",
     "parkingSpaces": "20", "operatingHours": "6-22"},
]
DEPOT = {"locationId": "loc-dal-depot", "name": "DAL Depot", "metro": "dal",
         "locationType": "depot", "latitude": "32.78", "longitude": "-96.80",
         "parkingSpaces": "50", "operatingHours": "5-21"}

_PLACE = {"loc-atl-downtown": 6, "loc-atl-airport": 2, "loc-atl-hub1": 2,
          "loc-aus-downtown": 5, "loc-aus-hub2": 2}


def vin(n: int) -> str:
    """A 17-character VIN in CMS's pattern (``^[A-HJ-NPR-Z0-9]{17}$``)."""
    return f"MRDAZ01S0WT{n:06d}"


def _rental_vehicles() -> list[dict]:
    out, n = [], 1
    for loc, count in _PLACE.items():
        for _ in range(count):
            # vehicleId differs from the VIN, as on staging (VEH-..., memory: coinciding ids
            # hide id hops).
            out.append({"vehicleId": f"VEH-RNT-{n:04d}", "vin": vin(n), "fleetId": RENTAL,
                        "locationId": loc})
            n += 1
    return out


RENTAL_VEHICLES = _rental_vehicles()
DELIVERY_VEHICLES = [{"vehicleId": f"VEH-DLV-{k:04d}", "vin": vin(900 + k), "fleetId": DELIVERY,
                      "locationId": DEPOT["locationId"]} for k in (1, 2)]
#: An iOS test user's car (drivers row with cognitoEmail) and a live-roster car, both at a
#: surplus location so the agent may recommend moving them (7.5's ``--part cms`` C0 must
#: exclude them; its tests pin ROSTER_VIN inside the ATL airport move).
IOS_HELD = "VEH-RNT-0001"
ROSTER_VIN = vin(2)
#: ``--part auto``'s world (task 8.4) puts its live-roster car at loc-aus-hub2 (a DEFICIT
#: location) instead, so the agent never recommends moving it and no default Finding carries
#: a roster car; P2 applies the full C0 check to every car of B. Tests that need a roster car
#: inside a Finding add the entry themselves (test_p2_b_with_roster_car_is_not_chosen).
AUTO_ROSTER_VIN = vin(16)

GAPS = {  # location -> (status, short, spare, supply, forecast_hours, util)
    "loc-atl-downtown": ("surplus", 0, 4, 6, 30.0, 0.30),
    "loc-atl-airport": ("deficit", 2, 0, 2, 60.0, 1.30),
    "loc-atl-hub1": ("deficit", 1, 0, 2, 45.0, 1.10),
    "loc-aus-downtown": ("surplus", 0, 3, 5, 25.0, 0.35),
    "loc-aus-hub2": ("deficit", 2, 0, 2, 55.0, 1.25),
}


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _policy_value() -> str:
    from agents.fi_rebalancing.tests.policy_file import policy_entry
    v = policy_entry()["staging_default"]
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))


# ---------------------------------------------------------------------------
# The rebalancing agent's ADP reads (fixture), as fi_rebal_world.agent_reads serves them
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _agent_reads(today: dt.date) -> Iterator[None]:
    import inspect
    from unittest import mock

    from agents.fi_rebalancing import sources as src

    d = today.isoformat()
    gaps = [{"location_id": loc, "status": s, "vehicles_short": short, "vehicles_spare": spare,
             "supply_vehicles": sup, "forecast_hours": fh, "forecast_utilization_pct": u,
             "data_as_of": d} for loc, (s, short, spare, sup, fh, u) in GAPS.items()]
    forecast = [{"location_id": loc, "forecast_date": (today + dt.timedelta(days=k)).isoformat(),
                 "forecast_hours": g[4], "method": "ml", "mape_28d": 0.10, "run_date": d,
                 "data_as_of": d} for loc, g in GAPS.items() for k in range(1, 10)]
    vd = [{"vin": v["vin"], "location_id": v["locationId"], "available_hours": 10.0}
          for v in RENTAL_VEHICLES]
    util = [{"vin": v["vin"], "snapshot_date": d, "available_hours": 10.0, "on_rent_hours": 0.0}
            for v in RENTAL_VEHICLES]
    patches = {
        "read_gaps": gaps, "read_forecast_days": forecast, "read_outcomes": [],
        "read_vehicle_days": vd, "read_vehicle_utilization": util,
        "read_busy_windows": [], "read_events": [], "read_open_ros": [],
        "read_service_soon": [],
        "read_request_lengths": [{"location_id": loc, "mean_request_hours": 24.0}
                                 for loc, g in GAPS.items() if g[0] == "deficit"],
        "read_grounding": (False, False),
        "read_open_items": ([], False),
    }

    def bound(real: Callable, value: Any) -> Callable:
        def fake(*a: Any, **kw: Any) -> Any:
            inspect.signature(real).bind(*a, **kw)
            return value
        return fake

    with contextlib.ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(src, name, bound(getattr(src, name), value)))
        yield


def run_agent(session: Any, today: dt.date) -> dict:
    """One rules-path run of the real agent with the real writers (no model)."""
    from agents.tier2 import runner
    from agents.tier2.artifact_writer import ArtifactWriter
    from agents.tier2.run_records import write_run_record

    ddb = session.client("dynamodb", region_name=REGION)
    res = session.resource("dynamodb", region_name=REGION)
    with _agent_reads(today):
        return runner.run_rebalance(
            STAGE, athena_client=object(), workgroup=f"cvx-{STAGE}-analytics",
            results_s3_path="s3://results/tier2/fi-rebalancing/", ddb_client=ddb,
            findings_table=FINDINGS, vehicles_table=VEHICLES, groundings_table=GROUNDINGS,
            fleet_locations_table=LOCS,
            avx_writer=ArtifactWriter(table_name=FINDINGS, dynamodb_client=ddb),
            run_record_writer=lambda **kw: write_run_record(ddb_client=res, table_name=RUNS, **kw),
            ssm_client=session.client("ssm", region_name=REGION))


# ---------------------------------------------------------------------------
# CMS's five refresh queries (fixture rows; every value a string, two rows per page)
# ---------------------------------------------------------------------------


def refresh_rows(today: dt.date) -> dict[str, list[dict[str, str | None]]]:
    last = (today - dt.timedelta(days=1)).isoformat()
    util = [{"vin": v["vin"], "location_id": v["locationId"], "last_day": last,
             "on_rent_h_1": "6.0", "avail_h_1": "16.0", "on_rent_h_7": "40.0",
             "avail_h_7": "112.0", "on_rent_h_30": "170.0", "avail_h_30": "480.0",
             "on_rent_last": "6.0", "avail_last": "16.0"} for v in RENTAL_VEHICLES]
    demand = [{"location_id": loc, "forecast_date": (today + dt.timedelta(days=k)).isoformat(),
               "run_date": today.isoformat(), "forecast_hours": str(g[4]),
               "pickup_hours": str(g[4] / 2), "method": "ml", "mape_28d": "0.1",
               "data_as_of": last} for loc, g in GAPS.items() for k in range(1, 8)]
    gaps = [{"location_id": loc, "horizon_days": str(h), "forecast_hours": str(g[4] * h / 7),
             "supply_vehicles": str(g[3]), "supply_hours": str(g[3] * 16.0 * h),
             "forecast_utilization_pct": str(g[5]), "status": g[0],
             "vehicles_short": str(g[1]), "vehicles_spare": str(g[2]), "data_as_of": last,
             "materialised_on": today.isoformat(), "snapshot_date": today.isoformat()}
            for loc, g in GAPS.items() for h in (7, 30)]
    return {"vehicle_utilization_daily": util, "fleet_bookings_forecast": demand,
            "supply_demand_gaps_snapshot": gaps, "move_outcomes": [],
            # Fifth read (G9-D4, CMS 75007152b): one row per delivery vehicle, using the same
            # column names as vehicle_utilization_daily (trip_hours aliased as on_rent_hours).
            "depot_utilization_daily": [
                {"vin": v["vin"], "location_id": v["locationId"], "fleet_id": v["fleetId"],
                 "last_day": last,
                 "on_rent_h_1": "6.0", "avail_h_1": "16.0",
                 "on_rent_h_7": "40.0", "avail_h_7": "112.0",
                 "on_rent_h_30": "170.0", "avail_h_30": "480.0",
                 "on_rent_last": "6.0", "avail_last": "16.0"}
                for v in DELIVERY_VEHICLES
            ]}


class RefreshAthena:
    """Athena for CMS's refresh: answers each of the five SQL texts from :func:`refresh_rows`."""

    def __init__(self, today: dt.date) -> None:
        self.rows = refresh_rows(today)
        self.queries: list[str] = []
        self._results: dict[str, list[list[str | None]]] = {}

    def start(self, p: dict) -> dict:
        sql = p["QueryString"]
        self.queries.append(sql)
        table = next((t for t in self.rows if f".{t}" in sql), None)
        if table is None:
            raise AwsError("InvalidRequestException", f"world: unexpected query {sql[:80]}")
        rows = self.rows[table]
        cols = list(rows[0]) if rows else ["move_id"]
        qid = str(uuid.uuid4())
        self._results[qid] = [cols] + [[r.get(c) for c in cols] for r in rows]
        return {"QueryExecutionId": qid}

    def get(self, p: dict) -> dict:
        return {"QueryExecution": {"QueryExecutionId": p["QueryExecutionId"],
                                   "Status": {"State": "SUCCEEDED"}}}

    def results(self, p: dict) -> dict:
        rows = self._results[p["QueryExecutionId"]]
        start = int(p.get("NextToken") or 0)
        page = rows[start:start + 2]
        out: dict = {"ResultSet": {"Rows": [{"Data": [{} if v is None else {"VarCharValue": v}
                                                      for v in r]} for r in page],
                                   "ResultSetMetadata": {"ColumnInfo": [
                                       {"Name": c, "Type": "varchar"} for c in rows[0]]}}}
        if start + 2 < len(rows):
            out["NextToken"] = str(start + 2)
        return out


# ---------------------------------------------------------------------------
# HTTPS REST APIs in front of the real handlers
# ---------------------------------------------------------------------------


def make_cert(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 (CA:TRUE, so it is its own trust anchor)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fi-rebal-cms-world")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "world-cert.pem", directory / "world-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def _claims_as_strings(claims: dict[str, Any]) -> dict[str, str]:
    """REST API Cognito authorizer claims: every value a string, groups comma-joined."""
    out: dict[str, str] = {}
    for k, v in claims.items():
        if k == "cognito:groups" and isinstance(v, list):
            out[k] = ",".join(v)
        elif isinstance(v, bool):
            out[k] = "true" if v else "false"
        else:
            out[k] = str(v)
    return out


class TlsApi:
    """One REST API stage on https://127.0.0.1:<port>/<stage>/ (``COGNITO_USER_POOLS``).

    Unknown route or method: 403 ``Missing Authentication Token``; no or bad token: 401;
    a handler exception: 502. Every request is recorded (method, route, path, body, status,
    response) so tests can assert what the script actually sent.
    """

    def __init__(self, name: str, stage: str, verify: Callable[[str], dict | None],
                 cert: tuple[Path, Path]) -> None:
        self.name, self.stage = name, stage
        self.routes: list[tuple[str, str, Any, Callable]] = []
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._verify = verify
        api = self

        class _H(http.server.BaseHTTPRequestHandler):
            def _any(self) -> None:
                api._serve(self)

            do_GET = do_POST = do_PUT = do_DELETE = _any

            def log_message(self, *a: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert[0]), str(cert[1]))
        self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"https://127.0.0.1:{self._server.server_port}/{stage}"

    def add(self, method: str, template: str, fn: Callable[[dict], dict]) -> None:
        import re
        pat = re.compile("^" + re.sub(r"\{([^}/]+)\}", r"(?P<\1>[^/]+)", template) + "$")
        self.routes.append((method, template, pat, fn))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _send(self, h: Any, status: int, raw: bytes) -> None:
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def _serve(self, h: Any) -> None:
        parsed = urllib.parse.urlsplit(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length).decode() if length else None
        rec: dict[str, Any] = {"method": h.command, "path": parsed.path, "query": parsed.query,
                               "body": body, "authorization": h.headers.get("Authorization")}
        with self.lock:
            self.requests.append(rec)
        prefix = f"/{self.stage}"
        rel = parsed.path[len(prefix):] if parsed.path.startswith(prefix + "/") else None
        hit = None
        for method, template, pat, fn in self.routes:
            m = pat.match(rel or "")
            if m and method == h.command:
                hit = (template, {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}, fn)
                break
        if hit is None:
            rec["status"] = 403
            return self._send(h, 403, b'{"message": "Missing Authentication Token"}')
        auth = (h.headers.get("Authorization") or "").strip()
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else auth
        claims = self._verify(token) if token else None
        if claims is None:
            rec["status"] = 401
            return self._send(h, 401, b'{"message": "Unauthorized"}')
        template, params, fn = hit
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        event = {
            "resource": template, "path": rel, "httpMethod": h.command,
            "headers": dict(h.headers.items()),
            "queryStringParameters": {k: v[-1] for k, v in qs.items()} or None,
            "pathParameters": params or None,
            "requestContext": {"resourcePath": template, "httpMethod": h.command,
                               "stage": self.stage, "requestId": str(uuid.uuid4()),
                               "authorizer": {"claims": _claims_as_strings(claims)}},
            "body": body, "isBase64Encoded": False,
        }
        rec["route"] = template
        try:
            resp = fn(event)
        except Exception:  # noqa: BLE001 — a Lambda error is API Gateway's 502
            rec["status"], rec["error"] = 502, traceback.format_exc()
            return self._send(h, 502, b'{"message": "Internal server error"}')
        status = int(resp.get("statusCode", 200))
        raw = resp.get("body") or b""
        raw = raw.encode() if isinstance(raw, str) else raw
        rec["status"], rec["response"] = status, raw.decode(errors="replace")[:4000]
        self._send(h, status, raw)

    def sent(self, method: str | None = None, route: str | None = None) -> list[dict]:
        with self.lock:
            return [r for r in self.requests
                    if (method is None or r["method"] == method)
                    and (route is None or r.get("route") == route)]


# ---------------------------------------------------------------------------
# Code under test: CMS from its commit, AVX's approve route from this tree
# ---------------------------------------------------------------------------

_CMS_TREE: dict[str, Any] = {}


def cms_tree() -> Path:
    """CMS at :data:`CMS_COMMIT` (FI service + the action-queue module), once per process."""
    if "path" not in _CMS_TREE:
        out = Path(tempfile.mkdtemp(prefix=f"fi-rebal-cms-{CMS_COMMIT}-"))
        # Removed at process exit: every pytest process (each battery mutant) made one of
        # these and none was ever deleted (715 dirs, 2.1 GB on 2026-10-24, with the disk full).
        import atexit, shutil  # noqa: E401, PLC0415
        atexit.register(shutil.rmtree, out, ignore_errors=True)
        raw = subprocess.run(
            ["git", "-C", str(CMS_REPO), "archive", CMS_COMMIT, "services/fleet_intelligence",
             "modules/cms_ui/source/handlers/main_api/fi_action_queue.py"],
            check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            tf.extractall(out, filter="data")
        _CMS_TREE["path"] = out
    return _CMS_TREE["path"]


def _load_file(path: Path, name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Clock:
    def epoch(self) -> float:
        import time
        return time.time()

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)


class _GroundingsProxy:
    """CMS ``groundings`` as ``moves`` sees it, with ``open_groundings_for_vehicle`` replaced
    (a defect switch on the move route only: return to service keeps the real check)."""

    def __init__(self, real: Any, open_fn: Callable) -> None:
        self._real, self.open_groundings_for_vehicle = real, open_fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _ddb(v: Any) -> Any:
    from boto3.dynamodb.types import TypeSerializer
    if isinstance(v, float):
        v = Decimal(str(v))
    return TypeSerializer().serialize(v)


def env_for(cms_url: str, avx_url: str, cert: Path, state_dir: Path) -> dict[str, str]:
    return {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION,
        "SSL_CERT_FILE": str(cert), "REQUESTS_CA_BUNDLE": str(cert),
        "LIVE_TESTS": "1", "FI_REBAL_LV_STATE_DIR": str(state_dir),
        # CMS FI Lambda (live env names, 2026-10-21)
        "VEHICLES_TABLE_NAME": VEHICLES, "GROUNDINGS_TABLE_NAME": GROUNDINGS,
        "VEHICLE_MOVES_TABLE_NAME": MOVES, "FLEET_LOCATIONS_TABLE_NAME": LOCS,
        "FLEETS_TABLE_NAME": FLEETS,
        "AVX_ACTIONS_TABLE_NAME": ACTIONS, "AVX_FINDINGS_TABLE_NAME": FINDINGS,
        "AVX_AGENT_RUNS_TABLE_NAME": RUNS, "AVX_APPROVAL_RULES_TABLE_NAME": RULES,
        "ATHENA_WORKGROUP": "cms-staging-analytics",
        "ATHENA_OUTPUT_LOC": f"s3://{CMS_RESULTS}/fleet-intelligence/",
        "ADP_REGION": ADP_REGION, "ADP_STAGE": STAGE, "DEPLOYMENT_STAGE": STAGE,
        # main_api (fi_action_queue) and the AVX findings Lambda
        "AVX_API_ENDPOINT": avx_url + "/", "AVX_STAGE": STAGE,
        "AVX_FINDINGS_TABLE": FINDINGS, "AVX_ACTIONS_TABLE": ACTIONS,
        "AVX_FLEET_IDS_CLAIM_TRUSTED": "true", "VSA_CMS_API_ENDPOINT": cms_url + "/",
    }


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class CmsWorld:
    """One fake staging. ``with CmsWorld(tmp_path) as w: rc, out, err = w.run_main([...])``."""

    #: The car on the live simulator roster (see ROSTER_VIN / AUTO_ROSTER_VIN).
    roster_vin = ROSTER_VIN

    def __init__(self, tmp: Path, *, recall_blocking: frozenset[str] = frozenset(),
                 recall_age_h: float = 0.0, recall_missing: frozenset[str] = frozenset()) -> None:
        self.tmp = Path(tmp)
        self.today = _today()
        self.recall_blocking = recall_blocking   # VINs given an open do-not-drive recall
        self.recall_age_h = recall_age_h         # how old the published recall views are
        self.recall_missing = recall_missing     # VINs left out of the recall cohort
        #: Defect switches (see the module docstring). Tests set them after open().
        self.defects: dict[str, Any] = {
            "cms_scope_open": False,        # CMS FI routes skip their fleet check
            "queue_scope_open": False,      # CMS's approve proxy skips check 1
            "moves_ignore_groundings": False,  # CMS POST /moves skips check 2
            "moves_groundings_unreadable": False,  # CMS POST /moves can't read groundings
            "rts_failures": 0,              # return-to-service answers 503 this many times
            "rts_lies": False,              # return-to-service answers 200, closes nothing
            "depot_gateway_403": False,     # the gateway answers 403 Forbidden to the depot user
            "cms_refuses_all": False,       # every FI read answers CMS's fleet 403
            "avx_cms_unreachable": False,   # AVX's executor can't reach CMS
            "executor_400": False,          # CMS answers 400 to the executor's own /moves body
            "avx_cms_unreachable_for": None,   # ... only for this finding id's approve
            "grounding_times_out": False,   # the groundings route answers 504 and writes nothing
            "grounding_commits_then_504": False,  # grounding is committed, then 504 (lost response)
            "controls_fail_after_move": False,  # the rental operator's repeats answer 503
            "probe_writes_action": False,   # the depot approve probe touches the Action
            "moves_list_drops": False,      # GET /moves drops every non-seed row
            "after_approve": None,          # callable(finding_id) run after each approve
            # True: the groundings and RTS routes skip the fleet check; "rts": only RTS does
            "groundings_scope_open": False,
            # route template -> seconds added per request (a number, or a list consumed in
            # order, 0 after), on the fake clock tests install
            "delay": {},
        }
        self.fake_time: Any = None          # set by tests that measure latency
        self.athena: Any = None
        self._env_saved: dict[str, str | None] = {}
        self._stack = contextlib.ExitStack()

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "CmsWorld":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def open(self) -> None:
        import boto3
        from moto import mock_aws

        self.state_dir = self.tmp / "state"
        self.cert = make_cert(self.tmp)
        for k in [k for k in os.environ if k.startswith("AWS_")]:
            self._setenv(k, None)
        self._setenv("AWS_ACCESS_KEY_ID", "testing")
        self._setenv("AWS_SECRET_ACCESS_KEY", "testing")
        self._setenv("AWS_DEFAULT_REGION", REGION)
        self._stack.enter_context(mock_aws())
        self.session = boto3.Session(region_name=REGION)
        self.router = Router()
        self.router.install()
        self.pool = FakeUserPool(_Clock(), region=REGION)
        self.pool.register(self.router)
        self.router.add("cognito-idp", "ListUserPools", lambda p: {"UserPools": [
            {"Id": f"{REGION}_OtherPool1", "Name": f"vsa-{STAGE}-voice"},
            {"Id": self.pool.pool_id, "Name": POOL_NAME}]})
        self.refresh_athena = RefreshAthena(self.today)
        self.athena = self.refresh_athena
        for op, attr in (("StartQueryExecution", "start"), ("GetQueryExecution", "get"),
                         ("GetQueryResults", "results")):
            self.router.add("athena", op, lambda p, a=attr: getattr(self.athena, a)(p))
        self._create()
        self._seed()
        self.agent_run = run_agent(self.session, self.today)
        self._load_code()
        self.cms = TlsApi("cms-staging-ui-api", "prod", self.pool.verify, self.cert)
        self.avx = TlsApi("vsa-staging-api", "staging", self.pool.verify, self.cert)
        for k, v in env_for(self.cms.url, self.avx.url, self.cert[0], self.state_dir).items():
            self._setenv(k, v)
        self._reset_avx_caches()
        out = self.ix.handler({"fleetIntelligenceTask": "refresh-rebalance-cache"}, None)
        assert out.get("status") == "refreshed", out
        self._publish_recall_views()
        self._routes()
        self.mod = self._load_script()

    def close(self) -> None:
        for api in (getattr(self, "cms", None), getattr(self, "avx", None)):
            if api is not None:
                api.close()
        if getattr(self, "router", None) is not None:
            self.router.uninstall()
        self._stack.close()
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _setenv(self, k: str, v: str | None) -> None:
        if k not in self._env_saved:
            self._env_saved[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # -- AWS resources (live key schemas, describe-table 2026-10-21) -------
    def _create(self) -> None:
        ddb = self.session.client("dynamodb", region_name=REGION)
        s = lambda n: {"AttributeName": n, "AttributeType": "S"}  # noqa: E731

        def gsi(name: str, pk: str, sk: str | None = None, proj: str = "ALL") -> dict:
            return {"IndexName": name, "Projection": {"ProjectionType": proj},
                    "KeySchema": [{"AttributeName": pk, "KeyType": "HASH"}]
                    + ([{"AttributeName": sk, "KeyType": "RANGE"}] if sk else [])}

        def table(name: str, keys: list[str], attrs: list[str], gsis: list[dict] | None = None) -> None:
            kw: dict = {"TableName": name, "BillingMode": "PAY_PER_REQUEST",
                        "AttributeDefinitions": [s(a) for a in attrs],
                        "KeySchema": [{"AttributeName": k, "KeyType": t}
                                      for k, t in zip(keys, ("HASH", "RANGE"))]}
            if gsis:
                kw["GlobalSecondaryIndexes"] = gsis
            ddb.create_table(**kw)

        # vin-index is KEYS_ONLY live (describe-table 2026-10-21; CMS storage_stack.py says
        # "do not widen"): a reader must GetItem the base row for anything but the keys.
        table(VEHICLES, ["vehicleId"], ["vehicleId", "vin"],
              [gsi("vin-index", "vin", proj="KEYS_ONLY")])
        table(FLEETS, ["fleetId"], ["fleetId"])
        table(GROUNDINGS, ["vehicleId", "actionId"], ["vehicleId", "actionId", "openFleetId",
                                                     "groundedAt"],
              [gsi("open-groundings-index", "openFleetId", "groundedAt")])
        table(MOVES, ["fleetId", "moveId"], ["fleetId", "moveId"])
        table(LOCS, ["fleetId", "locationId"], ["fleetId", "locationId"])
        table(DRIVERS, ["driverId"], ["driverId"])
        table(LIVE_PLAN, ["planDate", "sk"], ["planDate", "sk"])
        table(FINDINGS, ["finding_id"],
              ["finding_id", "owner_id", "vin", "vehicle_id", "fleet_id", "dealership_id",
               "district_id", "agent_id", "computed_at", "finding_key"],
              [gsi("by_owner", "owner_id"), gsi("by_vin", "vin"), gsi("by_vehicle", "vehicle_id"),
               gsi("by_fleet", "fleet_id"), gsi("by_dealership", "dealership_id"),
               gsi("by_district", "district_id"),
               gsi("by_agent_computed_at", "agent_id", "computed_at"),
               gsi("by_finding_key", "finding_key")])
        table(ACTIONS, ["action_id"],
              ["action_id", "target_system", "created_by", "fleet_id", "updated_at",
               "target_ref", "finding_id"],
              [gsi("by_target_system", "target_system"), gsi("by_created_by", "created_by"),
               gsi("by_fleet", "fleet_id", "updated_at"), gsi("by_target_ref", "target_ref"),
               gsi("by_finding", "finding_id")])
        table(RUNS, ["agent_component", "started_at"], ["agent_component", "started_at"])
        table(RULES, ["fleet_id", "rule_id"], ["fleet_id", "rule_id"])
        self.session.client("s3", region_name=ADP_REGION).create_bucket(Bucket=CMS_RESULTS)
        self.session.client("ssm", region_name=REGION).put_parameter(
            Name=POLICY_PARAM, Value=_policy_value(), Type="String")

    def _seed(self) -> None:
        ddb = self.session.client("dynamodb", region_name=REGION)
        for fleet, ftype in FLEET_TYPES.items():
            ddb.put_item(TableName=FLEETS, Item={"fleetId": {"S": fleet}, "fleetType": {"S": ftype}})
        for fleet, locs in ((RENTAL, LOCATIONS), (DELIVERY, [DEPOT])):
            for loc in locs:
                item = {"fleetId": {"S": fleet}}
                for k, v in loc.items():
                    item[k] = ({"N": v} if k in ("latitude", "longitude", "parkingSpaces")
                               else {"S": v})
                item["locationName"] = {"S": loc["name"]}
                ddb.put_item(TableName=LOCS, Item=item)
        for v in RENTAL_VEHICLES + DELIVERY_VEHICLES:
            ddb.put_item(TableName=VEHICLES, Item={k: {"S": x} for k, x in v.items()})
            # The bookings job's window-start baseline (decidedByKind seed), as on staging.
            ddb.put_item(TableName=MOVES, Item={
                "fleetId": {"S": v["fleetId"]}, "moveId": {"S": f"seed-{v['vehicleId']}"},
                "decidedByKind": {"S": "seed"}, "toLocationId": {"S": v["locationId"]},
                "vins": {"L": [{"S": v["vin"]}]}, "createdAt": {"S": "2026-09-01T00:00:00Z"},
                "effectiveDate": {"S": "2026-09-01"}})
        ddb.put_item(TableName=DRIVERS, Item={
            "driverId": {"S": "DRV-WORLD-IOS"}, "fleetId": {"S": RENTAL},
            "assignedVehicleId": {"S": IOS_HELD}, "cognitoEmail": {"S": "ios-user@example.com"}})
        ddb.put_item(TableName=DRIVERS, Item={
            "driverId": {"S": "DRV-RNT-WORLD1"}, "fleetId": {"S": RENTAL},
            "assignedVehicleId": {"S": "VEH-RNT-0003"}})   # a member driver, no app login
        roster_vid = next(v["vehicleId"] for v in RENTAL_VEHICLES if v["vin"] == self.roster_vin)
        ddb.put_item(TableName=LIVE_PLAN, Item={   # CMS plan_store.put_roster_entry's shape
            "planDate": {"S": "roster"}, "sk": {"S": roster_vid},
            "vin": {"S": self.roster_vin}, "vehicleId": {"S": roster_vid},
            "fleetId": {"S": RENTAL}, "state": {"S": "live"}})


    # -- code ------------------------------------------------------------
    def _load_code(self) -> None:
        tree = cms_tree()
        if str(tree) not in sys.path:
            sys.path.insert(0, str(tree))
        import services.fleet_intelligence.index as ix  # noqa: PLC0415
        import services.fleet_intelligence.lifecycle_cache as lc  # noqa: PLC0415
        import services.fleet_intelligence.moves as mv  # noqa: PLC0415
        assert Path(ix.__file__).is_relative_to(tree), ix.__file__   # never the working tree
        self.ix, self.lc, self.mv = ix, lc, mv
        self.queue = _load_file(
            tree / "modules/cms_ui/source/handlers/main_api/fi_action_queue.py",
            "fi_rebal_world_fi_action_queue")
        lam = REPO / "lambdas"
        for p in (str(REPO), str(lam)):
            if p not in sys.path:
                sys.path.insert(0, p)
        self.findings = _load_file(lam / "api-avx-findings" / "handler.py",
                                   "fi_rebal_world_avx_findings")

    def _reset_avx_caches(self) -> None:
        """Module caches from an earlier world or another test file (memory
        ``botocore-before-call-fake``): the findings handler's resource and both import names
        of ``_lib.avx_scope``."""
        for name in ("lambdas._lib.avx_scope", "_lib.avx_scope"):
            m = sys.modules.get(name)
            if m is not None:
                if hasattr(m, "_FINDINGS_TABLE"):
                    m._FINDINGS_TABLE = FINDINGS
                if hasattr(m, "_ddb"):
                    m._ddb = None
        f = self.findings
        f._FINDINGS_TABLE, f._ACTIONS_TABLE, f._STAGE = FINDINGS, ACTIONS, STAGE
        for attr in ("_ddb",):
            if hasattr(f, attr):
                setattr(f, attr, None)

    def _publish_recall_views(self) -> None:
        """Recall views by VIN shard, with the real ``lifecycle_cache.publish``: every fleet
        VIN in the cohort; a VIN in ``recall_blocking`` gets an open do-not-drive campaign."""
        lc = self.lc
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=self.recall_age_h)
        camp = {"campaign_id": "MRD-2026-901", "recall_class": "safety", "do_not_drive": True,
                "park_outside": False, "remedy_available_from": "2027-01-01",
                "received_date": "2026-10-01", "component": "Brake", "safety_text": "x"}
        shards: dict[int, list[str]] = {}
        for v in RENTAL_VEHICLES + DELIVERY_VEHICLES:
            shard = int.from_bytes(hashlib.sha256(v["vin"].encode()).digest()[:4], "big") % 16
            shards.setdefault(shard, []).append(v["vin"])
        views = []
        for shard, vins in shards.items():
            blocked = [x for x in vins if x in self.recall_blocking]
            body = {"computed_at": now.isoformat(),
                    "cohort": [{"vin": x} for x in vins if x not in self.recall_missing],
                    "campaigns": [camp] if blocked else [],
                    "vehicle_status": [{"vin": x, "campaign_id": camp["campaign_id"],
                                        "remedied": False, "match_basis": "CONFIRMED",
                                        "fleet_id": RENTAL} for x in blocked],
                    "fleet_summary": []}
            views.append((lc.KIND_RECALL, f"vin-shard-{shard:02d}", {"shard": shard}, body))
        lc.publish(0, views, computed_at=now, kinds=[lc.KIND_RECALL], fleets=[RENTAL, DELIVERY],
                   root=lc.recall_views_root())

    # -- routes ----------------------------------------------------------
    def _routes(self) -> None:
        fi = "/api/v1/fleet-intelligence"
        for path in ("locations", "utilization", "demand", "gaps", "moves"):
            self.cms.add("GET", f"{fi}/{path}", self._cms_fi)
        self.cms.add("POST", f"{fi}/moves", self._cms_fi)
        self.cms.add("POST", f"{fi}/groundings", self._cms_fi)
        self.cms.add("POST", f"{fi}/groundings/{{vehicleId}}/return-to-service", self._cms_rts)
        self.cms.add("POST", f"{fi}/actions/{{findingId}}/approve", self._cms_queue)
        self.cms.add("POST", f"{fi}/actions/{{findingId}}/decline", self._cms_queue)
        self.avx.add("POST", "/findings/{finding_id}/approve", self._avx_findings)
        self.avx.add("POST", "/findings/{finding_id}/decline", self._avx_findings)

    def _delay(self, event: dict) -> None:
        secs = self.defects["delay"].get(event["resource"])
        if isinstance(secs, list):
            secs = secs.pop(0) if secs else 0
        if secs and self.fake_time is not None:
            self.fake_time.sleep(secs)

    @staticmethod
    def _fleet_of(event: dict) -> str:
        return event["requestContext"]["authorizer"]["claims"].get("custom:fleetIds", "")

    def _cms_fi(self, event: dict) -> dict:
        from unittest import mock
        self._delay(event)
        is_move = event["httpMethod"] == "POST" and event["resource"].endswith("/moves")
        if (self.defects["depot_gateway_403"] and event["httpMethod"] == "GET"
                and self._fleet_of(event) == DELIVERY):
            return {"statusCode": 403, "body": json.dumps({"message": "Forbidden"})}
        if self.defects["cms_refuses_all"] and event["httpMethod"] == "GET":
            return {"statusCode": 403,
                    "body": json.dumps({"error": "Not authorized for the requested fleet scope"})}
        if is_move and self.defects["executor_400"] and "findingId" in json.loads(event["body"] or "{}"):
            return {"statusCode": 400, "body": json.dumps({"error": "world: executor body refused"})}
        if (self.defects["grounding_times_out"] and event["httpMethod"] == "POST"
                and event["resource"].endswith("/groundings")):
            return {"statusCode": 504, "body": json.dumps({"message": "Endpoint request timed out"})}
        if (self.defects["grounding_commits_then_504"] and event["httpMethod"] == "POST"
                and event["resource"].endswith("/groundings")):
            # Commit the grounding (as a real service would before a gateway timeout), then
            # return 504 — the "lost response" case that C2's fix must handle.
            self.ix.handler(event, None)
            return {"statusCode": 504, "body": json.dumps({"message": "Endpoint request timed out"})}
        if (getattr(self, "auto_defects", {}).get("u4_grounding_post_writes_nothing")
                and event["httpMethod"] == "POST"
                and event["resource"].endswith("/groundings")):
            # S4 bullet 3: return 201 without writing any DDB row — simulates a
            # successful HTTP response whose DDB commit was lost (different from C2's
            # grounding_commits_then_504 which commits first then returns 504).
            self.auto_defects["u4_grounding_post_writes_nothing"] = False  # single-shot
            return {"statusCode": 201, "body": json.dumps({"message": "grounding created"})}
        if (is_move and self.defects["controls_fail_after_move"] and self._fleet_of(event) == RENTAL
                and self._any_move()):
            return {"statusCode": 503, "body": json.dumps({"message": "Service Unavailable"})}
        if (event["httpMethod"] == "GET" and event["resource"].endswith("/moves")
                and self.defects["moves_list_drops"]):
            resp = self.ix.handler(event, None)
            body = json.loads(resp["body"])
            body["rows"] = []
            return {**resp, "body": json.dumps(body)}
        if (event["httpMethod"] == "GET" and event["resource"].endswith("/moves")
                and getattr(self, "auto_defects", {}).get("u5_move_tolocation_wrong")):
            self.auto_defects["u5_move_tolocation_wrong"] = False  # single-shot
            resp = self.ix.handler(event, None)
            body = json.loads(resp["body"])
            for r in (body.get("rows") or []):
                if "toLocationId" in r:
                    r["toLocationId"] = "loc-wrong-destination"
            return {**resp, "body": json.dumps(body)}
        # lane 12a — N-W4b: corrupt findingId in every row of GET /moves response.
        if (event["httpMethod"] == "GET" and event["resource"].endswith("/moves")
                and getattr(self, "auto_defects", {}).get("u5_move_wrong_findingId")):
            self.auto_defects["u5_move_wrong_findingId"] = False  # single-shot
            resp = self.ix.handler(event, None)
            body = json.loads(resp["body"])
            for r in (body.get("rows") or []):
                if "findingId" in r:
                    r["findingId"] = "FIND-wrong-finding-id-lane12a"
            return {**resp, "body": json.dumps(body)}
        with contextlib.ExitStack() as stack:
            if self.defects["cms_scope_open"]:
                stack.enter_context(mock.patch.object(self.ix, "_authorize_or_403",
                                                      lambda *a, **k: None))
                stack.enter_context(mock.patch.object(self.mv._auth, "authorize_fleet_write",
                                                      lambda *a, **k: (True, "world-defect")))
            gso = self.defects["groundings_scope_open"]
            if gso and event["httpMethod"] == "POST" and (
                    (gso is True and event["resource"].endswith("/groundings"))
                    or event["resource"].endswith("/return-to-service")):
                stack.enter_context(mock.patch.object(self.ix._auth, "authorize_fleet_write",
                                                      lambda *a, **k: (True, "world-defect")))
            if is_move and self.defects["moves_ignore_groundings"]:
                stack.enter_context(mock.patch.object(
                    self.mv, "gnd", _GroundingsProxy(self.mv.gnd, lambda *a, **k: [])))
            if is_move and self.defects["moves_groundings_unreadable"]:
                def unreadable(*a: Any, **k: Any) -> Any:
                    raise RuntimeError("world: groundings table unreadable")
                stack.enter_context(mock.patch.object(
                    self.mv, "gnd", _GroundingsProxy(self.mv.gnd, unreadable)))
            return self.ix.handler(event, None)

    def _cms_rts(self, event: dict) -> dict:
        rental = self._fleet_of(event) == RENTAL     # the cleanup's calls, not the depot probe
        if rental and self.defects["rts_failures"] > 0:
            self.defects["rts_failures"] -= 1
            return {"statusCode": 503, "body": json.dumps({"message": "Service Unavailable"})}
        if rental and self.defects["rts_lies"]:
            return {"statusCode": 200, "body": json.dumps({"returnedAt": "2026-10-21T00:00:00Z"})}
        return self._cms_fi(event)

    def _cms_queue(self, event: dict) -> dict:
        """main_api's dispatch to ``fi_action_queue.route`` (index.py 3166-3185 at the commit),
        with its Caller derived as main_api's handler derives it (lines 1978-2041)."""
        import boto3
        from unittest import mock
        claims = event["requestContext"]["authorizer"]["claims"]
        groups = [g.strip() for g in (claims.get("cognito:groups") or "").split(",") if g.strip()]
        fleets = [f.strip() for f in (claims.get("custom:fleetIds") or "").split(",") if f.strip()]
        is_admin = "platform-admin" in groups
        is_viewer = "fleet-viewer" in groups and "fleet-operator" not in groups
        is_guest = "fleet-guest" in groups
        caller = self.queue.Caller(
            groups=frozenset(groups), fleet_ids=tuple(fleets), unscoped=is_admin or is_viewer,
            read_only=is_viewer or is_guest or not any(g in groups for g in ("platform-admin",
                                                                              "fleet-operator")),
            sub=claims.get("sub", ""))
        fid = (event["pathParameters"] or {}).get("findingId", "")
        if self.defects["controls_fail_after_move"] and self._any_move() and fleets == [RENTAL]:
            return {"statusCode": 503, "body": json.dumps({"message": "Service Unavailable"})}
        if self.defects["probe_writes_action"] and self._any_move() and fleets == [DELIVERY]:
            self._res().Table(ACTIONS).update_item(
                Key={"action_id": self.decision_id(fid)}, UpdateExpression="SET updated_at = :u",
                ExpressionAttributeValues={":u": "2099-01-01T00:00:00Z"})
        with contextlib.ExitStack() as stack:
            if self.defects["queue_scope_open"]:
                stack.enter_context(mock.patch.object(self.queue, "_write_fleet_check",
                                                      lambda *a, **k: None))
            resp = self.queue.route(
                method=event["httpMethod"], path=event["path"],
                query=event["queryStringParameters"] or {}, raw_body=event["body"],
                headers=event["headers"], caller=caller,
                dynamodb=boto3.resource("dynamodb", region_name=REGION), environ=os.environ,
                opener=self._queue_opener())
        if callable(self.defects["after_approve"]) and event["path"].endswith("/approve"):
            self.defects["after_approve"](fid)
        return resp

    def _any_move(self) -> bool:
        return any(m.get("decidedByKind") != "seed" for m in self.items(MOVES))

    def _queue_opener(self) -> Callable:
        """main_api's own no-redirect opener, with this world's certificate as its trust anchor
        (the module's ``_OPENER`` fixed its TLS context when the module was first imported)."""
        import urllib.request
        ctx = ssl.create_default_context(cafile=str(self.cert[0]))
        return urllib.request.build_opener(self.queue._NoRedirect(),
                                           urllib.request.HTTPSHandler(context=ctx)).open

    def _avx_findings(self, event: dict) -> dict:
        only = self.defects["avx_cms_unreachable_for"]
        if self.defects["avx_cms_unreachable"] or (only and only in event["path"]):
            from unittest import mock
            with mock.patch.dict(os.environ, {"VSA_CMS_API_ENDPOINT": "https://127.0.0.1:1/prod/"}):
                return self.findings.handler(event, None)
        return self.findings.handler(event, None)

    # -- the script ------------------------------------------------------
    def _load_script(self) -> Any:
        path = REPO / "scripts" / "live-verify-fi-rebalancing.py"
        spec = importlib.util.spec_from_file_location(f"lv_fi_rebal_cms_{uuid.uuid4().hex}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Outside every write window (tests of the window move this clock).
        safe = dt.datetime.now(dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        mod._now = lambda: safe
        return mod

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        import signal
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = self.mod.main(argv)
        finally:
            for s, h in saved.items():
                signal.signal(s, h)
        return rc, out.getvalue(), err.getvalue()

    def argv(self, *extra: str) -> list[str]:
        return ["--part", "cms", "--stage", STAGE, "--workgroup", "wg",
                "--cms-api-endpoint", self.cms.url, *extra]


    # -- state, read back for assertions ------------------------------------
    def _res(self) -> Any:
        return self.session.resource("dynamodb", region_name=REGION)

    def items(self, table: str) -> list[dict]:
        t, out, kw = self._res().Table(table), [], {}
        while True:
            r = t.scan(**kw)
            out += r.get("Items", [])
            if not r.get("LastEvaluatedKey"):
                return out
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def vehicle(self, vehicle_id: str) -> dict:
        return self._res().Table(VEHICLES).get_item(Key={"vehicleId": vehicle_id},
                                                    ConsistentRead=True).get("Item") or {}

    def vehicle_id_of(self, v: str) -> str:
        return next(x["vehicleId"] for x in RENTAL_VEHICLES + DELIVERY_VEHICLES if x["vin"] == v)

    def move(self, action_id: str, fleet: str = RENTAL) -> dict | None:
        return self._res().Table(MOVES).get_item(Key={"fleetId": fleet, "moveId": action_id},
                                                 ConsistentRead=True).get("Item")

    @staticmethod
    def decision_id(finding_id: str) -> str:
        h = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
        return f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

    def action(self, finding_id: str) -> dict | None:
        return self._res().Table(ACTIONS).get_item(Key={"action_id": self.decision_id(finding_id)},
                                                   ConsistentRead=True).get("Item")

    def groundings(self, vehicle_id: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        return self._res().Table(GROUNDINGS).query(
            KeyConditionExpression=Key("vehicleId").eq(vehicle_id)).get("Items", [])

    def move_findings(self) -> list[dict]:
        return sorted((f for f in self.items(FINDINGS) if f.get("finding_kind") == "fleet.rebalance.move"),
                      key=lambda f: f["finding_id"])

    def put_finding(self, item: dict) -> None:
        self._res().Table(FINDINGS).put_item(Item=json.loads(json.dumps(item, default=str),
                                                             parse_float=Decimal))

    def user(self, fleet: str, group: str = "fleet-operator") -> str:
        """A pool user minted directly (for world self-tests); returns its ID token."""
        sub = str(uuid.uuid4())
        u = {"sub": sub, "attrs": {"email": f"world-{sub[:8]}@example.invalid",
                                   "custom:fleetIds": fleet}, "groups": {group},
             "password": None, "status": "CONFIRMED", "created": _Clock().now()}
        self.pool.users[sub] = u
        return self.pool.mint(u)


# ============================================================================
# AutoWorld — shared fake world for --part auto (stage E task 8.4)
# ============================================================================

# ---------------------------------------------------------------------------
# New constants
# ---------------------------------------------------------------------------

AUTO_APPROVER_ROLE = f"vsa-{STAGE}-avx-auto-approver-{REGION}"
AUTO_APPROVER_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{AUTO_APPROVER_ROLE}"
#: Fake access key ID used by the auto-approver when signing the internal-API call.
#: The IamTlsApi maps this to AUTO_APPROVER_USER_ARN so is_internal_caller() passes.
AUTO_APPROVER_KEY = "AUTOAPPROVERKEY000001"   # 20 chars, all UPPER/digit — looks like a real AKIA*
AUTO_APPROVER_SECRET = "fakeAutoApproverSecretKey000000000"
AUTO_APPROVER_SESSION = "fi-auto-session"
AUTO_APPROVER_USER_ARN = (
    f"arn:aws:sts::{ACCOUNT}:assumed-role/{AUTO_APPROVER_ROLE}/{AUTO_APPROVER_SESSION}"
)
BOUNDS_PARAM = f"/vsa/{STAGE}/avx/fi-rebalancing/auto-approval-bounds"
BOUNDS = {"max_limit_usd": 500, "max_vehicles_per_auto_move": 5}
AUTO_APPROVER_FN = f"vsa-{STAGE}-avx-auto-approver-{REGION}"
AUTO_APPROVER_STREAM_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
INTERNAL_API_ID = "yourapi000"  # scanner placeholder (was a real staging id)
INTERNAL_FI_MOVES_PATH = "/api/v1/fleet-intelligence/internal/moves"


# ---------------------------------------------------------------------------
# IamTlsApi — a REST API with IAM / SigV4 authentication instead of Cognito
# ---------------------------------------------------------------------------

class IamTlsApi:
    """An HTTPS REST API that enforces AWS_IAM (execute-api / SigV4) authentication.

    Unlike :class:`TlsApi`, the caller identity is derived from the ``Authorization``
    header's SigV4 ``Credential`` access-key-ID, not a Cognito Bearer token.  The
    resource-policy Deny is enforced here: only a session of *allowed_role_arn* passes;
    every other caller (including unsigned requests) gets 403 Forbidden.

    ``requestContext.identity.userArn`` is populated from the key → ARN mapping so the
    Lambda handler's ``is_internal_caller()`` works without verifying the actual HMAC.
    """

    def __init__(self, name: str, stage: str, cert: tuple, *,
                 allowed_role_arn: str,
                 key_to_arn: dict,
                 api_id: str = "",
                 region: str = "") -> None:
        self.name, self.stage = name, stage
        self._allowed_role_arn = allowed_role_arn
        self._key_to_arn: dict[str, str] = dict(key_to_arn)
        self._api_id = api_id
        self._region = region
        #: W8 defect: when True, every denied response returns the old-style
        #: ``{"message": "Forbidden"}`` body instead of AWS's real bodies.
        #: Tests set this to verify the script rejects non-specific 403 bodies.
        self.bare_deny: bool = False
        #: W8 defect: when non-empty, substitute this path in the explicit-deny
        #: resource ARN instead of the actual request path.  Tests set this to
        #: simulate the probe reaching the wrong API or stage, so the deny body
        #: names a different path and the script should FAIL.
        self.wrong_deny_path: str = ""
        #: W13 defect: when True, the signed-probe explicit-deny body omits
        #: 'with an explicit deny in a resource-based policy' so the script
        #: can verify it requires the phrase, not just the ARN.
        self.implicit_deny: bool = False
        #: C8 defect: when non-empty, substitute this string for self.stage in
        #: the explicit-deny resource ARN.  Tests use this to verify U1 requires
        #: the stage taken from the endpoint URL, not the VSA stage.
        self.wrong_deny_stage: str = ""
        self.routes: list = []
        self.requests: list = []
        self.lock = threading.Lock()
        api = self

        class _H(http.server.BaseHTTPRequestHandler):
            def _any(self) -> None:
                api._serve(self)

            do_GET = do_POST = do_PUT = do_DELETE = _any

            def log_message(self, *a: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert[0]), str(cert[1]))
        self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"https://127.0.0.1:{self._server.server_port}/{stage}"

    def add(self, method: str, template: str, fn: Callable) -> None:
        import re as _re  # noqa: PLC0415
        pat = _re.compile("^" + _re.sub(r"\{([^}/]+)\}", r"(?P<\1>[^/]+)", template) + "$")
        self.routes.append((method, template, pat, fn))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _send(self, h: Any, status: int, raw: bytes) -> None:
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    @staticmethod
    def _parse_sigv4_key(auth: str) -> str | None:
        """Extract the access-key-ID from a SigV4 Authorization header's Credential= clause."""
        import re as _re  # noqa: PLC0415
        m = _re.search(r"Credential=([A-Z0-9]+)/", auth)
        return m.group(1) if m else None

    def _user_arn(self, auth: str) -> str | None:
        """Map the SigV4 credential to an assumed-role ARN, or None for unsigned / unknown."""
        key = self._parse_sigv4_key(auth)
        return self._key_to_arn.get(key or "") if key else None

    def _passes_deny(self, user_arn: str | None) -> bool:
        """Enforce the resource-policy Deny: only the auto-approver role's sessions pass.

        A session ARN has the form ``arn:aws:sts::<account>:assumed-role/<role>/<session>``
        where ``<session>`` contains no ``/``.  Exactly mirroring CMS moves.is_internal_caller.
        """
        if not user_arn:
            return False
        import re as _re  # noqa: PLC0415
        m = _re.fullmatch(r"arn:aws:iam::(\d+):role/(.+)", self._allowed_role_arn)
        if not m:
            return False
        account, role_name = m.groups()
        prefix = f"arn:aws:sts::{account}:assumed-role/{role_name}/"
        session = user_arn[len(prefix):] if user_arn.startswith(prefix) else ""
        return bool(session) and "/" not in session

    def _serve(self, h: Any) -> None:
        parsed = urllib.parse.urlsplit(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length).decode() if length else None
        auth = h.headers.get("Authorization") or ""
        rec: dict[str, Any] = {"method": h.command, "path": parsed.path, "query": parsed.query,
                               "body": body, "authorization": auth}
        with self.lock:
            self.requests.append(rec)
        # W13: route-match first; AWS returns 403 "Missing Authentication Token" for
        # unknown routes regardless of authentication, so the deny only fires on matched routes.
        prefix = f"/{self.stage}"
        rel = parsed.path[len(prefix):] if parsed.path.startswith(prefix + "/") else None
        hit = None
        for method, template, pat, fn in self.routes:
            m_route = pat.match(rel or "")
            if m_route and method == h.command:
                hit = (template, {k: urllib.parse.unquote(v) for k, v in m_route.groupdict().items()}, fn)
                break
        if hit is None:
            rec["status"] = 403
            return self._send(h, 403, json.dumps(
                {"message": "Missing Authentication Token"}).encode())
        user_arn = self._user_arn(auth)
        if not self._passes_deny(user_arn):
            # W8: return AWS-style 403 bodies for matched routes:
            #   - unsigned: "Missing Authentication Token"
            #   - signed non-approver: explicit deny with resource ARN naming the request path
            # bare_deny defect: return the old non-AWS body so tests can catch it.
            rec["status"] = 403
            if self.bare_deny:
                return self._send(h, 403, json.dumps({"message": "Forbidden"}).encode())
            if not auth:
                return self._send(h, 403, json.dumps(
                    {"message": "Missing Authentication Token"}).encode())
            # Construct the explicit-deny resource ARN so U1 can verify the correct path.
            import re as _re  # noqa: PLC0415
            m_acct = _re.search(r"::(\d+):", self._allowed_role_arn)
            account = m_acct.group(1) if m_acct else "000000000000"
            prefix_stage = f"/{self.stage}"
            path_after_stage = (
                parsed.path[len(prefix_stage):]
                if parsed.path.startswith(prefix_stage + "/") or parsed.path == prefix_stage
                else parsed.path
            )
            # wrong_deny_path defect: simulate a probe that reached the wrong resource.
            if self.wrong_deny_path:
                path_after_stage = self.wrong_deny_path
            # C8 defect: substitute a different stage in the ARN so U1 must compare
            # the endpoint stage, not args.stage.
            _stage_in_arn = self.wrong_deny_stage if self.wrong_deny_stage else self.stage
            resource_arn = (
                f"arn:aws:execute-api:{self._region}:{account}:{self._api_id}"
                f"/{_stage_in_arn}/{h.command}{path_after_stage}"
            )
            caller = user_arn or "anonymous"
            deny_msg = (
                f"User: {caller} is not authorized to perform: execute-api:Invoke "
                f"on resource: {resource_arn} with an explicit deny in a resource-based policy"
            )
            if self.implicit_deny:
                deny_msg = (
                    f"User: {caller} is not authorized to perform: execute-api:Invoke "
                    f"on resource: {resource_arn}"
                )
            return self._send(h, 403, json.dumps({"Message": deny_msg}).encode())
        template, params, fn = hit
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        event = {
            "resource": template, "path": rel, "httpMethod": h.command,
            "headers": dict(h.headers.items()),
            "queryStringParameters": {k: v[-1] for k, v in qs.items()} or None,
            "pathParameters": params or None,
            # IAM-signed call: identity carries userArn; NO Cognito authorizer / claims.
            # is_internal_caller() in moves.py specifically checks authorizer.claims is absent.
            "requestContext": {
                "resourcePath": template, "httpMethod": h.command,
                "stage": self.stage, "requestId": str(uuid.uuid4()),
                "identity": {"userArn": user_arn},
            },
            "body": body, "isBase64Encoded": False,
        }
        rec["route"] = template
        try:
            resp = fn(event)
        except Exception:  # noqa: BLE001
            rec["status"], rec["error"] = 502, traceback.format_exc()
            return self._send(h, 502, b'{"message": "Internal server error"}')
        status = int(resp.get("statusCode", 200))
        raw = resp.get("body") or b""
        raw = raw.encode() if isinstance(raw, str) else raw
        rec["status"], rec["response"] = status, raw.decode(errors="replace")[:4000]
        self._send(h, status, raw)


# ---------------------------------------------------------------------------
# AutoWorld
# ---------------------------------------------------------------------------

class AutoWorld(CmsWorld):
    """Extends :class:`CmsWorld` for ``--part auto`` (task 8.4).

    Adds on top of the full CmsWorld:

    - **Bounds SSM parameter**: ``/vsa/{stage}/avx/fi-rebalancing/auto-approval-bounds``.
    - **Approval-rules routes** on the existing AVX Cognito TLS API
      (``lambdas/api-avx-approval-rules/handler.py``).
    - **Internal IAM TLS API** for CMS's ``/internal/moves`` route (``IamTlsApi``).
    - **Fake Lambda services**: ``GetFunctionConfiguration``, ``ListEventSourceMappings``,
      ``Invoke`` (runs the real ``avx-auto-approver/handler.py`` with the approver's creds),
      and ``apigateway:GetRestApi`` returning the Deny resource policy.

    Defect switches in :attr:`auto_defects` put expected failures into the fake responses
    so tests can assert the script detects them.
    """

    roster_vin = AUTO_ROSTER_VIN


    def open(self) -> None:
        super().open()          # full CmsWorld: tables, agent run, recall views, routes
        self._open_auto()

    def close(self) -> None:
        if getattr(self, "internal", None):
            self.internal.close()
        super().close()

    def _open_auto(self) -> None:
        """Add the auto-approver infrastructure after the CmsWorld is fully started."""
        from scripts.tests.fi_rw_world.awsfake import AwsError, streaming  # noqa: PLC0415

        # Load the auto-approver and approval-rules Lambda handlers.
        lam = REPO / "lambdas"
        self.auto_approver = _load_file(lam / "avx-auto-approver" / "handler.py",
                                        "fi_rebal_world_avx_auto_approver")
        self.approval_rules = _load_file(lam / "api-avx-approval-rules" / "handler.py",
                                         "fi_rebal_world_avx_approval_rules")
        self._reset_auto_approver_caches()
        self._reset_approval_rules_caches()

        # Bounds parameter in SSM (moto).
        self.session.client("ssm", region_name=REGION).put_parameter(
            Name=BOUNDS_PARAM,
            Value=json.dumps(BOUNDS, separators=(",", ":")),
            Type="String", Overwrite=True,
        )

        # IAM TLS API for CMS's internal /moves route (a separate API from CMSAPI, G8-D1).
        self.internal = IamTlsApi(
            f"cms-{STAGE}-ui-fi-internal-api", "prod",
            self.cert,
            allowed_role_arn=AUTO_APPROVER_ROLE_ARN,
            key_to_arn={AUTO_APPROVER_KEY: AUTO_APPROVER_USER_ARN},
            api_id=INTERNAL_API_ID,
            region=REGION,
        )
        self.internal.add("POST", INTERNAL_FI_MOVES_PATH, self._internal_moves)

        # Approval-rules routes on the existing Cognito AVX TLS API.
        self.avx.add("GET", "/approval-rules/fleet/{fleet_id}", self._avx_rules)
        self.avx.add("PUT", "/approval-rules/fleet/{fleet_id}/{rule_id}", self._avx_rules)
        self.avx.add("DELETE", "/approval-rules/fleet/{fleet_id}/{rule_id}", self._avx_rules)

        # Fake Lambda + API Gateway calls for P1's read-only checks and the actual invoke.
        self._streaming = streaming
        self.router.add("lambda", "GetFunctionConfiguration", self._fake_get_fn_config)
        self.router.add("lambda", "ListEventSourceMappings", self._fake_list_esm)
        self.router.add("lambda", "Invoke", self._fake_invoke)
        self.router.add("apigateway", "GetRestApi", self._fake_get_rest_api)
        # CloudFormation DescribeStacks fake: returns FiInternalApiEndpoint so P1's
        # endpoint-vs-CFN-output check can compare the Lambda's endpoint against CMS's
        # output (spec: "the endpoint equals CMS's FiInternalApiEndpoint output").
        internal_url = self.internal.url + "/"
        self.router.add("cloudformation", "DescribeStacks", lambda p: {
            "Stacks": [{
                "StackName": f"cms-{STAGE}-ui",
                "StackStatus": "UPDATE_COMPLETE",
                "Outputs": [
                    {"OutputKey": "FiInternalApiEndpoint", "OutputValue": internal_url},
                ],
            }]
        })

        # W11: DescribeStackResource fake so P1 can derive the API id when the endpoint
        # is not an execute-api URL (as in the world's https://127.0.0.1:<port>/prod/).
        def _fake_describe_stack_resource(p: dict) -> dict:
            if p.get("LogicalResourceId") == "FiInternalApi":
                return {"StackResourceDetail": {
                    "LogicalResourceId": "FiInternalApi",
                    "PhysicalResourceId": INTERNAL_API_ID,
                    "ResourceType": "AWS::ApiGateway::RestApi",
                    "ResourceStatus": "UPDATE_COMPLETE",
                }}
            raise KeyError(
                f"no fake for LogicalResourceId={p.get('LogicalResourceId')!r}")

        self.router.add("cloudformation", "DescribeStackResource",
                        _fake_describe_stack_resource)

        # Defect switches: put expected failures into fake responses for test assertions.
        self.auto_defects: dict[str, Any] = {
            "auto_moves_disabled": False,   # P1: FI_AUTO_MOVES_ENABLED missing/false
            "endpoint_wrong": False,        # P1: endpoint doesn't match internal API URL
            "timeout_short": False,         # P1: timeout = 60 (flag-off default)
            "stream_disabled": False,       # P1: stream mapping State="Disabled"
            "bounds_wrong": False,          # P1: bounds param has wrong values
            "bounds_param_missing": False,  # W11: AVX_AUTO_APPROVAL_BOUNDS_PARAM absent from Lambda env
            "bounds_param_wrong_name": False,  # W11: AVX_AUTO_APPROVAL_BOUNDS_PARAM has wrong path
            "has_enabled_rule": False,      # P1: an enabled move_vehicles rule already exists
            "policy_wrong_role": False,     # W7: Deny Condition names a different role ARN
            "policy_extra_allow": False,    # W7: policy has an extra Allow statement
            "policy_wrong_operator": False, # W11: Deny Condition uses StringEquals (not StringNotEquals)
            # C1 (rest) defects:
            "dispatch_fails": False,        # C1: _cms_internal_endpoint raises → Action status=failed
            # C4 defects:
            "u3_invoke_raises": False,      # C4: first Invoke raises (test U3-exception path)
            "delete_bounds_after_p1": False,  # C4(c): delete bounds SSM after P1 (bounds-unreadable at U6)
            # W1 defects:
            "delete_user_fails": False,     # W1: AdminDeleteUser raises (test cleanup-row failure)
            # W2 defects:
            "u3_batch_item_failure": False, # W2: U3 invoke returns batchItemFailures=[{itemIdentifier:...}]
            "u3_invoke_noop": False,        # W2: U3 invoke returns 200/[] without running the handler
            # W4 defects:
            "u5_wrong_decided_by_id": False,   # W4: on U5 (3rd invoke), corrupt Action's decided_by.id
            "u5_move_tolocation_wrong": False, # W4: GET /moves returns wrong toLocationId for A's move
            # W9 defects:
            "u3_evaluator_approves_anyway": False,  # W9: after U3 invoke, write Action for A (fail-open evaluator)
            "u2_create_raises": False,              # W9: first rules-API PUT raises (U2 exception path)
            "u4_invoke_raises": False,              # W9: 2nd invoke (U4) raises after grounding succeeds
            "u5_invoke_raises": False,              # W9: 3rd invoke (U5) raises
            # C6 defects: corrupt A's CMS move record after CMS writes it
            "u5_cms_move_decidedByKind_operator": False,  # C6: change decidedByKind to 'operator'
            "u5_cms_move_approverSub": False,             # C6: add approverSub to CMS move record
            # lane 12a: cycle-2 survivor defects
            "u5_cms_move_wrong_ruleId": False,            # F-C6c: corrupt ruleId in CMS move record
            "u5_cms_move_wrong_decision": False,          # F-C6d: corrupt decision in CMS move record
            "u5_move_wrong_findingId": False,             # N-W4b: corrupt findingId in GET /moves response
            "policy_wrong_principal": False,              # F-W11c: Deny names wrong Principal
            "policy_wrong_resource": False,               # F-W11d: Deny names wrong Resource (different API)
            # W14 defects: corrupt B's AVX Action after the auto-approver writes it (invoke 2)
            "u4_b_decision_other": False,           # W14: B Action decision != 'auto_approved'
            "u4_b_decided_by_other_rule": False,   # W14: B Action decided_by.id names a different rule
            "u4_b_status_not_declined": False,     # W14: B Action status != 'declined'
            "u4_b_created_before_run": False,      # W14: B Action created_at predates run start
            "u4_b_failure_kind_other": False,      # W14: last audit entry failure_kind != 'target_refused'
            "u4_b_error_status_other": False,      # W14: last audit entry error_status != 409
            # W12 defect: write an extra move record during _internal_moves (U5).
            # Value is the extra moveId string (or False).  Fires after A's successful
            # move write; the extra move is NOT in existing_move_ids (captured before U2).
            "u6_extra_move_record": False,
            # S4 bullet 2: write a second evaluator run record during the first
            # invoke (U3) to simulate a concurrent stream-triggered run landing in
            # the same [u3_start, u3_end] window.  Single-shot.
            "u3_extra_stream_run": False,
            # lane 13b: W16 — shift the auto-approver's _now_iso by this many seconds
            # (positive = ahead; negative = behind) during every invoke.  Simulates
            # operator / Lambda clock skew.  Not single-shot.
            "u3_approver_clock_skew_s": 0,
            # S4 bullet 3: grounding POST returns 201 without writing a DDB row —
            # simulates a successful HTTP response whose DDB commit was lost.
            # Single-shot (fires on the first grounding POST only).
            "u4_grounding_post_writes_nothing": False,
            # N-C4b defect: first pure-disable PUT (enabled=False, no limit) returns
            # 503 so U6 must retry.  Single-shot.
            "u6_first_disable_put_fails": False,
            # lane 12c: N-W1 — InitiateAuth raises after create succeeds so the
            # creation-failure path can be exercised with a non-empty cleanup list.
            # Single-shot so only the first call (the temp operator) is affected.
            "op_auth_fails": False,
            # lane 13c: R3-W17 — every pure-disable PUT (enabled=False, no limit)
            # returns 503 so U6 / cleanup-only never succeeds.  Persistent (not
            # single-shot), so all three retries are refused.
            "u6_all_disable_puts_fail": False,
        }
        self._auto_invoke_count: int = 0    # reset per world open; tracks U3/U4/U5 order

        # Override AdminDeleteUser to support the delete_user_fails defect: must be done
        # AFTER super().open() registers the FakeUserPool handlers on self.router.
        _pool_delete = self.pool.admin_delete_user

        def _delete_user_handler(p: dict) -> dict:
            if self.auto_defects.get("delete_user_fails"):
                from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
                raise AwsError("InternalErrorException", "world: delete_user_fails defect")
            return _pool_delete(p)

        self.router.add("cognito-idp", "AdminDeleteUser", _delete_user_handler)

        # lane 12c: N-W1 — override InitiateAuth so op_auth_fails (single-shot) makes
        # the first call raise AFTER the user has been created.  Combined with
        # delete_user_fails this puts an entry in the cleanup list so the
        # creation-failure path's "for cmd in cleanup" is exercised.
        _pool_auth = self.pool.initiate_auth

        def _initiate_auth_handler(p: dict) -> dict:
            if self.auto_defects.get("op_auth_fails"):
                from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
                self.auto_defects["op_auth_fails"] = False  # single-shot
                raise AwsError("InternalErrorException", "world: op_auth_fails defect")
            return _pool_auth(p)

        self.router.add("cognito-idp", "InitiateAuth", _initiate_auth_handler)

        # Additional env vars the auto-approver, approval-rules and CMS's internal route need.
        for k, v in self._auto_env().items():
            self._setenv(k, v)

    def _auto_env(self) -> dict[str, str]:
        return {
            # CmsExecutor reads this to build the SigV4-signed URL.
            "VSA_CMS_FI_INTERNAL_API_ENDPOINT": self.internal.url + "/",
            # CMS's is_internal_caller() checks userArn is a session of this role (G8-D3).
            "AVX_AUTO_APPROVER_ROLE_ARN": AUTO_APPROVER_ROLE_ARN,
            # Evaluator gate: only "true" enables the move kind (G8-D7/D9).
            "FI_AUTO_MOVES_ENABLED": "true",
            # SSM parameter name for the bounds.
            "AVX_AUTO_APPROVAL_BOUNDS_PARAM": BOUNDS_PARAM,
            # Auto-approver's own table env vars (different names from CMS's _TABLE_NAME keys).
            "AVX_RULES_TABLE": RULES,
            "AVX_AGENT_RUNS_TABLE": RUNS,
            # Approval-rules Lambda table env var.
            "AVX_APPROVAL_RULES_TABLE": RULES,
        }

    def _reset_auto_approver_caches(self) -> None:
        aa = self.auto_approver
        aa._ddb = None
        aa._ssm = None

    def _reset_approval_rules_caches(self) -> None:
        ar = self.approval_rules
        ar._ddb = None
        ar._ssm = None
        ar._RULES_TABLE = RULES

    # -- route handlers --------------------------------------------------

    def _internal_moves(self, event: dict) -> dict:
        """POST /internal/moves behind the IamTlsApi — invokes CMS's handler.

        Applies the same ``moves_ignore_groundings`` and ``moves_groundings_unreadable``
        defects as ``_serve_fi`` so ``test_u4_grounding_check_is_real_not_vacuous``
        can test the case where CMS's grounding check is bypassed on the internal route.
        """
        import contextlib  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415
        with contextlib.ExitStack() as stack:
            if self.defects["moves_ignore_groundings"]:
                stack.enter_context(mock.patch.object(
                    self.mv, "gnd", _GroundingsProxy(self.mv.gnd, lambda *a, **k: [])))
            if self.defects["moves_groundings_unreadable"]:
                def unreadable(*a: Any, **k: Any) -> Any:
                    raise RuntimeError("world: groundings table unreadable")
                stack.enter_context(mock.patch.object(
                    self.mv, "gnd", _GroundingsProxy(self.mv.gnd, unreadable)))
            result = self.ix.handler(event, None)
        # C6: defects to corrupt A's CMS move record after CMS writes it.
        # Only fire on a successful (2xx) move response so we don't consume the defect
        # on U4's 409 grounding-refusal call before A's successful move in U5.
        _result_status = int(result.get("statusCode", 0))
        if 200 <= _result_status < 300:
            if self.auto_defects.get("u5_cms_move_decidedByKind_operator"):
                self.auto_defects["u5_cms_move_decidedByKind_operator"] = False  # single-shot
                try:
                    for _m in self._res().Table(MOVES).scan().get("Items", []):
                        if _m.get("decidedByKind") == "rule":
                            self._res().Table(MOVES).update_item(
                                Key={"fleetId": _m["fleetId"], "moveId": _m["moveId"]},
                                UpdateExpression="SET decidedByKind = :k",
                                ExpressionAttributeValues={":k": "operator"})
                except Exception:  # noqa: BLE001
                    pass
            if self.auto_defects.get("u5_cms_move_approverSub"):
                self.auto_defects["u5_cms_move_approverSub"] = False  # single-shot
                try:
                    for _m in self._res().Table(MOVES).scan().get("Items", []):
                        if _m.get("decidedByKind") == "rule":
                            self._res().Table(MOVES).update_item(
                                Key={"fleetId": _m["fleetId"], "moveId": _m["moveId"]},
                                UpdateExpression="SET approverSub = :a",
                                ExpressionAttributeValues={":a": "test-approver-sub"})
                except Exception:  # noqa: BLE001
                    pass
            # lane 12a — F-C6c: corrupt ruleId in CMS move record to a wrong value.
            if self.auto_defects.get("u5_cms_move_wrong_ruleId"):
                self.auto_defects["u5_cms_move_wrong_ruleId"] = False  # single-shot
                try:
                    for _m in self._res().Table(MOVES).scan().get("Items", []):
                        if _m.get("decidedByKind") == "rule":
                            self._res().Table(MOVES).update_item(
                                Key={"fleetId": _m["fleetId"], "moveId": _m["moveId"]},
                                UpdateExpression="SET ruleId = :r",
                                ExpressionAttributeValues={":r": "wrong-fleet/wrong-rule-id"})
                except Exception:  # noqa: BLE001
                    pass
            # lane 12a — F-C6d: corrupt decision in CMS move record to a wrong value.
            if self.auto_defects.get("u5_cms_move_wrong_decision"):
                self.auto_defects["u5_cms_move_wrong_decision"] = False  # single-shot
                try:
                    for _m in self._res().Table(MOVES).scan().get("Items", []):
                        if _m.get("decidedByKind") == "rule":
                            self._res().Table(MOVES).update_item(
                                Key={"fleetId": _m["fleetId"], "moveId": _m["moveId"]},
                                UpdateExpression="SET decision = :d",
                                ExpressionAttributeValues={":d": "manual_approved"})
                except Exception:  # noqa: BLE001
                    pass
            # W12 defect: write an extra move record to the moves table after A's
            # successful move.  The extra move is NOT in existing_move_ids (captured
            # before U2), so U6's residue check should flag it.
            extra_move_id = self.auto_defects.get("u6_extra_move_record")
            if extra_move_id and isinstance(extra_move_id, str):
                self.auto_defects["u6_extra_move_record"] = False  # single-shot
                import datetime as _dt  # noqa: PLC0415
                _now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    self._res().Table(MOVES).put_item(Item={
                        "fleetId": RENTAL, "moveId": extra_move_id,
                        "fromLocationId": "loc-extra-src",
                        "toLocationId": "loc-extra-dst",
                        "decidedByKind": "rule",
                        "createdAt": _now, "updatedAt": _now,
                    })
                except Exception:  # noqa: BLE001
                    pass
        return result

    def _avx_rules(self, event: dict) -> dict:
        """Approval-rules routes behind the Cognito AVX API."""
        self._reset_approval_rules_caches()
        # C4(c): delete the bounds SSM param just before the first disable PUT reaches
        # the approval-rules Lambda. The Lambda's pure-disable path (no 'limit' in body)
        # skips the SSM read; the old path (with 'limit') tries to read bounds and fails.
        if self.auto_defects.get("delete_bounds_after_p1"):
            body = json.loads(event.get("body") or "{}")
            if event.get("httpMethod") == "PUT" and body.get("enabled") is False:
                self.auto_defects["delete_bounds_after_p1"] = False  # single-shot
                try:
                    self.session.client("ssm", region_name=REGION).delete_parameter(
                        Name=BOUNDS_PARAM)
                except Exception:  # noqa: BLE001
                    pass
        # W9: u2_create_raises — raise on the first rules-API PUT (simulates U2 create
        # PUT failing with an unexpected exception before the rule is created).
        if (self.auto_defects.get("u2_create_raises")
                and event.get("httpMethod") == "PUT"):
            self.auto_defects["u2_create_raises"] = False  # single-shot
            raise RuntimeError("world: u2_create_raises defect")
        # N-C4b defect: first pure-disable PUT (enabled=False, no limit) returns 503.
        # Matches U6's disable (no limit) but not U2's create (has limit).  Single-shot.
        if self.auto_defects.get("u6_first_disable_put_fails") and event.get("httpMethod") == "PUT":
            body = json.loads(event.get("body") or "{}")
            if body.get("enabled") is False and "limit" not in body:
                self.auto_defects["u6_first_disable_put_fails"] = False
                return {"statusCode": 503, "body": json.dumps({"message": "transient error"})}
        # lane 13c: R3-W17 — every pure-disable PUT returns 503 (persistent; not single-shot).
        if self.auto_defects.get("u6_all_disable_puts_fail") and event.get("httpMethod") == "PUT":
            body = json.loads(event.get("body") or "{}")
            if body.get("enabled") is False and "limit" not in body:
                return {"statusCode": 503, "body": json.dumps({"message": "transient error"})}
        return self.approval_rules.handler(event, None)

    # -- fake AWS call handlers ------------------------------------------

    def _fake_get_fn_config(self, params: dict) -> dict:
        """Fake lambda:GetFunctionConfiguration — auto-approver's expected live config.

        Defect switches control deviations that P1 must detect.
        """
        from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
        fn = params.get("FunctionName", "")
        if AUTO_APPROVER_FN not in fn:
            raise AwsError("ResourceNotFoundException", f"Function not found: {fn}")
        enabled = ("false" if self.auto_defects["auto_moves_disabled"] else "true")
        endpoint = ("https://wrong-api-id.execute-api.us-east-2.amazonaws.com/prod/"
                    if self.auto_defects["endpoint_wrong"]
                    else self.internal.url + "/")
        timeout = (60 if self.auto_defects["timeout_short"] else 300)
        return {
            "FunctionName": AUTO_APPROVER_FN,
            "FunctionArn": (
                f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{AUTO_APPROVER_FN}"),
            "Role": AUTO_APPROVER_ROLE_ARN,
            "Timeout": timeout,
            "Environment": {
                "Variables": {
                    "FI_AUTO_MOVES_ENABLED": enabled,
                    "VSA_CMS_FI_INTERNAL_API_ENDPOINT": endpoint,
                    **(
                        {}
                        if self.auto_defects.get("bounds_param_missing")
                        else {
                            "AVX_AUTO_APPROVAL_BOUNDS_PARAM": (
                                f"/vsa/{STAGE}/avx/fi-rebalancing/WRONG-bounds"
                                if self.auto_defects.get("bounds_param_wrong_name")
                                else BOUNDS_PARAM
                            )
                        }
                    ),
                    "AVX_RULES_TABLE": RULES,
                    "AVX_ACTIONS_TABLE": ACTIONS,
                    "AVX_AGENT_RUNS_TABLE": RUNS,
                }
            },
        }

    def _fake_list_esm(self, params: dict) -> dict:
        """Fake lambda:ListEventSourceMappings — the findings-stream mapping."""
        state = ("Disabled" if self.auto_defects["stream_disabled"] else "Enabled")
        result = {
            "EventSourceMappings": [{
                "UUID": AUTO_APPROVER_STREAM_UUID,
                "FunctionArn": (
                    f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{AUTO_APPROVER_FN}"),
                "EventSourceArn": (
                    f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{FINDINGS}"
                    "/stream/2026-10-01T00:00:00.000"),
                "State": state,
            }]
        }
        return result

    def _fake_invoke(self, params: dict) -> dict:
        """Fake lambda:Invoke — runs the auto-approver handler with the approver's creds.

        Temporarily sets ``AWS_ACCESS_KEY_ID = AUTO_APPROVER_KEY`` (and companions)
        so ``CmsExecutor._lambda_credentials()`` returns frozen credentials whose access
        key the :class:`IamTlsApi` maps to ``AUTO_APPROVER_USER_ARN``.  This makes the
        SigV4-signed POST pass the IAM API's Deny-policy check.

        Also patches ``agents.executors.cms._cms_internal_endpoint`` to return the world's
        fake internal API URL (which is ``https://127.0.0.1:{port}/prod``, not an
        execute-api URL).  Without this patch the CmsExecutor's URL validation raises
        ``ValueError`` and every dispatch ends with ``status=failed``.
        """
        # C4: single-shot defect — raise on the first invoke (simulates a U3 exception).
        if self.auto_defects["u3_invoke_raises"]:
            self.auto_defects["u3_invoke_raises"] = False  # reset so later invokes work
            raise RuntimeError("world: u3_invoke_raises defect")
        from scripts.tests.fi_rw_world.awsfake import AwsError, streaming  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415
        fn = params.get("FunctionName", "")
        if AUTO_APPROVER_FN not in fn:
            raise AwsError("ResourceNotFoundException", f"Function not found: {fn}")
        # W2: u3_invoke_noop — return success without running the handler (no run record written).
        if self.auto_defects.get("u3_invoke_noop"):
            self.auto_defects["u3_invoke_noop"] = False  # single-shot: only affects the first invoke
            payload = json.dumps({"batchItemFailures": []}).encode()
            return {"StatusCode": 200, "FunctionError": "", "Payload": streaming(payload)}
        # W2: u3_batch_item_failure — return a non-empty batchItemFailures for the first invoke.
        if self.auto_defects.get("u3_batch_item_failure"):
            self.auto_defects["u3_batch_item_failure"] = False  # single-shot
            payload = json.dumps(
                {"batchItemFailures": [{"itemIdentifier": "seq-0001"}]}).encode()
            return {"StatusCode": 200, "FunctionError": "", "Payload": streaming(payload)}
        raw = params.get("Payload")
        event = json.loads(raw) if isinstance(raw, (str, bytes)) else {}
        self._auto_invoke_count += 1
        # W9: u4_invoke_raises — fires on the 2nd invoke (U4), after the grounding POST
        # has already succeeded.  Simulates an exception inside U4 after the probe car is
        # grounded; U6's finally must still disable the rule and return the probe car.
        if self._auto_invoke_count == 2 and self.auto_defects.get("u4_invoke_raises"):
            self.auto_defects["u4_invoke_raises"] = False  # single-shot
            raise RuntimeError("world: u4_invoke_raises defect")
        # W9: u5_invoke_raises — fires on the 3rd invoke (U5).  Simulates an exception
        # inside U5; U6's finally must still disable the rule and return the probe car.
        if self._auto_invoke_count == 3 and self.auto_defects.get("u5_invoke_raises"):
            self.auto_defects["u5_invoke_raises"] = False  # single-shot
            raise RuntimeError("world: u5_invoke_raises defect")
        saved = {k: os.environ.get(k)
                 for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
        try:
            # Give the auto-approver the special signing identity.
            os.environ["AWS_ACCESS_KEY_ID"] = AUTO_APPROVER_KEY
            os.environ["AWS_SECRET_ACCESS_KEY"] = AUTO_APPROVER_SECRET
            os.environ["AWS_SESSION_TOKEN"] = f"fakesession-{AUTO_APPROVER_SESSION}"
            self._reset_auto_approver_caches()
            # Patch _cms_internal_endpoint so the URL-validation accepts the world's URL.
            # C1 (rest): dispatch_fails makes the endpoint raise ValueError so @never_raises
            # returns terminal_error → Action status = "failed", not "declined".
            internal_base = self.internal.url.rstrip("/")
            if self.auto_defects.get("dispatch_fails"):
                cms_endpoint_ctx = mock.patch(
                    "agents.executors.cms._cms_internal_endpoint",
                    side_effect=ValueError("world: dispatch_fails — endpoint resolution fails"),
                )
            else:
                cms_endpoint_ctx = mock.patch(
                    "agents.executors.cms._cms_internal_endpoint",
                    return_value=(internal_base, REGION),
                )
            # lane 13b: W16 — apply clock skew if requested.  Replace _now_iso on the
            # module object so the handler's started_at is offset by the requested delta.
            _skew_s = self.auto_defects.get("u3_approver_clock_skew_s", 0)
            if _skew_s:
                import datetime as _dtimp2  # noqa: PLC0415
                _delta = _dtimp2.timedelta(seconds=_skew_s)
                _orig_now_iso = self.auto_approver._now_iso

                def _skewed_now_iso() -> str:
                    t = _dtimp2.datetime.fromisoformat(
                        _orig_now_iso().replace("Z", "+00:00"))
                    return (
                        (t + _delta)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )

                self.auto_approver._now_iso = _skewed_now_iso
                try:
                    with cms_endpoint_ctx:
                        result = self.auto_approver.handler(event, None)
                finally:
                    self.auto_approver._now_iso = _orig_now_iso
            else:
                with cms_endpoint_ctx:
                    result = self.auto_approver.handler(event, None)
            # W4: on U5 (third invoke), corrupt the Action's decided_by.id so the script
            # detects the wrong id.  Done AFTER the handler writes the record.
            if self._auto_invoke_count == 3 and self.auto_defects.get("u5_wrong_decided_by_id"):
                self.auto_defects["u5_wrong_decided_by_id"] = False  # single-shot
                try:
                    records = (event.get("Records") or [{}])
                    fid = (records[0].get("dynamodb") or {}).get(
                        "NewImage", {}).get("finding_id", {}).get("S", "")
                    if fid:
                        act_id = self.decision_id(fid)
                        tbl = self.session.resource(
                            "dynamodb", region_name=REGION).Table(ACTIONS)
                        act_item = tbl.get_item(
                            Key={"action_id": act_id},
                            ConsistentRead=True).get("Item")
                        if act_item:
                            wrong_db = dict(act_item.get("decided_by") or {})
                            wrong_db["id"] = "wrong-fleet/wrong-rule"
                            tbl.update_item(
                                Key={"action_id": act_id},
                                UpdateExpression="SET decided_by = :d",
                                ExpressionAttributeValues={":d": wrong_db},
                            )
                except Exception:  # noqa: BLE001
                    pass
            # W14: on U4 (second invoke), corrupt B's Action to trip one specific check.
            # Done AFTER the handler writes B's Action; each defect is single-shot and
            # corrupts exactly the field it names so tests can isolate each U4 check.
            if self._auto_invoke_count == 2:
                records_b = (event.get("Records") or [{}])
                fid_b = (records_b[0].get("dynamodb") or {}).get(
                    "NewImage", {}).get("finding_id", {}).get("S", "")
                if fid_b:
                    act_id_b = self.decision_id(fid_b)
                    tbl_b = self.session.resource(
                        "dynamodb", region_name=REGION).Table(ACTIONS)
                    # W14-1: B Action decision != 'auto_approved'
                    if self.auto_defects.get("u4_b_decision_other"):
                        self.auto_defects["u4_b_decision_other"] = False
                        try:
                            tbl_b.update_item(
                                Key={"action_id": act_id_b},
                                UpdateExpression="SET #d = :v",
                                ExpressionAttributeNames={"#d": "decision"},
                                ExpressionAttributeValues={":v": "auto_declined"})
                        except Exception:  # noqa: BLE001
                            pass
                    # W14-2: B Action decided_by.id names a different rule
                    if self.auto_defects.get("u4_b_decided_by_other_rule"):
                        self.auto_defects["u4_b_decided_by_other_rule"] = False
                        try:
                            act_item_b = tbl_b.get_item(
                                Key={"action_id": act_id_b},
                                ConsistentRead=True).get("Item")
                            if act_item_b:
                                wrong_db = dict(act_item_b.get("decided_by") or {})
                                wrong_db["id"] = f"{RENTAL}/other-rule-id"
                                tbl_b.update_item(
                                    Key={"action_id": act_id_b},
                                    UpdateExpression="SET decided_by = :d",
                                    ExpressionAttributeValues={":d": wrong_db})
                        except Exception:  # noqa: BLE001
                            pass
                    # W14-3: B Action status != 'declined'
                    if self.auto_defects.get("u4_b_status_not_declined"):
                        self.auto_defects["u4_b_status_not_declined"] = False
                        try:
                            tbl_b.update_item(
                                Key={"action_id": act_id_b},
                                UpdateExpression="SET #s = :v",
                                ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues={":v": "executed"})
                        except Exception:  # noqa: BLE001
                            pass
                    # W14-4: B Action created_at predates run start
                    if self.auto_defects.get("u4_b_created_before_run"):
                        self.auto_defects["u4_b_created_before_run"] = False
                        try:
                            tbl_b.update_item(
                                Key={"action_id": act_id_b},
                                UpdateExpression="SET created_at = :v",
                                ExpressionAttributeValues={":v": "2020-01-01T00:00:00Z"})
                        except Exception:  # noqa: BLE001
                            pass
                    # W14-5: last audit entry failure_kind != 'target_refused'
                    if self.auto_defects.get("u4_b_failure_kind_other"):
                        self.auto_defects["u4_b_failure_kind_other"] = False
                        try:
                            act_item_b = tbl_b.get_item(
                                Key={"action_id": act_id_b},
                                ConsistentRead=True).get("Item")
                            if act_item_b:
                                trail = list(act_item_b.get("audit_trail") or [])
                                if trail:
                                    trail[-1] = dict(trail[-1])
                                    trail[-1]["failure_kind"] = "timeout"
                                    tbl_b.update_item(
                                        Key={"action_id": act_id_b},
                                        UpdateExpression="SET audit_trail = :t",
                                        ExpressionAttributeValues={":t": trail})
                        except Exception:  # noqa: BLE001
                            pass
                    # W14-6: last audit entry error_status != 409
                    if self.auto_defects.get("u4_b_error_status_other"):
                        self.auto_defects["u4_b_error_status_other"] = False
                        try:
                            act_item_b = tbl_b.get_item(
                                Key={"action_id": act_id_b},
                                ConsistentRead=True).get("Item")
                            if act_item_b:
                                trail = list(act_item_b.get("audit_trail") or [])
                                if trail:
                                    trail[-1] = dict(trail[-1])
                                    trail[-1]["error_status"] = 404
                                    tbl_b.update_item(
                                        Key={"action_id": act_id_b},
                                        UpdateExpression="SET audit_trail = :t",
                                        ExpressionAttributeValues={":t": trail})
                        except Exception:  # noqa: BLE001
                            pass
            # W9: u3_evaluator_approves_anyway — after the 1st invoke (U3), write the
            # Action for A directly to DDB to simulate a fail-open evaluator that approved
            # a move above the rule's limit.  The script's ConsistentRead check must detect
            # the Action and record U3 FAIL.
            if self._auto_invoke_count == 1 and self.auto_defects.get("u3_evaluator_approves_anyway"):
                self.auto_defects["u3_evaluator_approves_anyway"] = False  # single-shot
                try:
                    import datetime as _dtimp  # noqa: PLC0415
                    _now_iso = _dtimp.datetime.now(_dtimp.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    records = (event.get("Records") or [{}])
                    fid = (records[0].get("dynamodb") or {}).get(
                        "NewImage", {}).get("finding_id", {}).get("S", "")
                    if fid:
                        act_id = self.decision_id(fid)
                        self._res().Table(ACTIONS).put_item(Item={
                            "action_id": act_id,
                            "finding_id": fid,
                            "fleet_id": RENTAL,
                            "decision": "auto_approved",
                            "status": "executed",
                            "decided_by": {
                                "kind": "rule",
                                "id": f"{RENTAL}/livetest-fi-rebal-auto",
                            },
                            "created_at": _now_iso,
                            "updated_at": _now_iso,
                        })
                except Exception:  # noqa: BLE001
                    pass
            # S4 bullet 2: after U3's invoke (count=1) runs the handler and writes
            # its run record, write a second run record to simulate a concurrent
            # stream-triggered evaluator run landing in the same [u3_start, u3_end]
            # window.  Single-shot so later invokes are not affected.
            # sleep(1) advances the real clock so the extra record gets a distinct
            # DDB key (different started_at second) AND so that u3_end (captured after
            # the invoke returns) is >= the extra record's timestamp.
            if self._auto_invoke_count == 1 and self.auto_defects.get("u3_extra_stream_run"):
                self.auto_defects["u3_extra_stream_run"] = False
                try:
                    import datetime as _dt  # noqa: PLC0415
                    import time as _time    # noqa: PLC0415
                    from agents.tier2.run_records import write_run_record as _wrr  # noqa: PLC0415
                    _time.sleep(1)  # advance the clock so the extra record gets a NEW second
                    _now = (
                        _dt.datetime.now(_dt.timezone.utc)
                        .replace(microsecond=0)
                        .isoformat().replace("+00:00", "Z")
                    )
                    _wrr(
                        agent_id="agent-fi-evaluator",
                        component="evaluator",
                        started_at=_now,
                        finished_at=_now,
                        status="completed",
                        artifacts_written=0,
                        duplicates_skipped=0,
                        agent_version="stream-triggered-test",
                        # write_run_record requires a boto3 DynamoDB *resource*, not client
                        ddb_client=self.session.resource("dynamodb", region_name=REGION),
                        table_name=RUNS,
                    )
                except Exception:  # noqa: BLE001
                    pass
            payload = json.dumps(result).encode()
            return {"StatusCode": 200, "FunctionError": "", "Payload": streaming(payload)}
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"errorMessage": str(exc),
                              "errorType": type(exc).__name__}).encode()
            return {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": streaming(err)}
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _fake_get_rest_api(self, params: dict) -> dict:
        """Fake apigateway:GetRestApi — returns the internal API's Deny resource policy.

        W7: honours the ``restApiId`` parameter in the Resource field of the Deny statement
        so P1's exact policy comparison can verify the Resource scopes to this API.
        Defect switches ``policy_wrong_role``, ``policy_extra_allow``, and
        ``policy_wrong_operator`` let tests drive P1's exact-comparison checks.
        W11: policy is returned as backslash-escaped JSON (live format); Principal is
        {"AWS": "*"} and Resource uses the real region/account (not wildcards).
        """
        rest_api_id = params.get("restApiId") or INTERNAL_API_ID
        deny_role = (
            "arn:aws:iam::123456789012:role/wrong-role"
            if self.auto_defects.get("policy_wrong_role")
            else AUTO_APPROVER_ROLE_ARN
        )
        cond_operator = (
            "StringEquals"
            if self.auto_defects.get("policy_wrong_operator")
            else "StringNotEquals"
        )
        # lane 12a — F-W11c: wrong Principal (not {"AWS": "*"}).
        deny_principal: Any = (
            {"AWS": "arn:aws:iam::123456789012:user/wrong-principal"}
            if self.auto_defects.get("policy_wrong_principal")
            else {"AWS": "*"}
        )
        # lane 12a — F-W11d: wrong Resource (different API id → script's exact check fails).
        deny_resource = (
            f"arn:aws:execute-api:{REGION}:{ACCOUNT}:wrongapi0000/*"
            if self.auto_defects.get("policy_wrong_resource")
            else f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{rest_api_id}/*"
        )
        stmts = [{
            "Effect": "Deny",
            "Principal": deny_principal,
            "Action": "execute-api:Invoke",
            "Resource": deny_resource,
            "Condition": {
                cond_operator: {"aws:PrincipalArn": deny_role}
            }
        }]
        if self.auto_defects.get("policy_extra_allow"):
            stmts.append({
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": "execute-api:Invoke",
                "Resource": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{rest_api_id}/*",
            })
        policy = {"Version": "2012-10-17", "Statement": stmts}
        policy_json = json.dumps(policy, separators=(",", ":"))
        escaped = policy_json.replace('"', '\\"')
        return {
            "id": rest_api_id,
            "name": f"cms-{STAGE}-ui-fi-internal-api",
            "policy": escaped,
        }

    # -- run_main / argv overrides ---------------------------------------

    def run_main(self, argv: list) -> tuple:  # type: ignore[override]
        """Like CmsWorld.run_main but converts SystemExit (argparse rejects unknown --part)
        to a ``(rc, out, err)`` tuple so test assertions fail with AssertionError, not
        SystemExit.  Unlike CmsWorld.run_main, captured stdout/stderr are preserved even
        when SystemExit is raised (C7: run-id print and cleanup pointer remain visible).
        """
        import signal as _signal
        saved = {s: _signal.getsignal(s)
                 for s in (_signal.SIGINT, _signal.SIGTERM, _signal.SIGHUP)}
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = self.mod.main(argv)
            return rc, out.getvalue(), err.getvalue()
        except SystemExit as exc:
            return (int(exc.code) if exc.code is not None else 2), out.getvalue(), err.getvalue()
        finally:
            for s, h in saved.items():
                _signal.signal(s, h)

    def auto_argv(self, *extra: str) -> list:
        """Base argv for --part auto. --allow-writes is included for U2-U6 write items.
        Additional flags for cleanup or specific run IDs can be passed as *extra*.
        """
        return [
            "--part", "auto",
            "--stage", STAGE,
            "--workgroup", "wg",                  # required by existing argparse
            "--cms-api-endpoint", self.cms.url,   # CMS Cognito API (U4 grounding, residue checks)
            "--avx-api-endpoint", self.avx.url,   # AVX Cognito API (approval-rules)
            "--allow-writes",
            "--limit", "500",    # max_limit_usd; all fake Findings have cost < $174.80
            "--cms-results-bucket", CMS_RESULTS,  # C3/W5: bucket for _recall_clear in P2
            *extra,
        ]

    # -- state helpers ---------------------------------------------------

    def put_rule(self, fleet_id: str, rule_id: str, *,
                 action_kind: str = "move_vehicles",
                 enabled: bool = False,
                 limit: float = 400.0) -> None:
        """Write an approval rule directly to DynamoDB (bypasses the API routes)."""
        item = {
            "fleet_id": fleet_id, "rule_id": rule_id,
            "action_kind": action_kind, "enabled": enabled,
            "limit": Decimal(str(limit)), "created_by": "world-test",
            "updated_at": "2026-10-22T00:00:00Z",
        }
        self._res().Table(RULES).put_item(Item=item)

    def get_rule(self, fleet_id: str, rule_id: str) -> dict | None:
        return self._res().Table(RULES).get_item(
            Key={"fleet_id": fleet_id, "rule_id": rule_id}, ConsistentRead=True).get("Item")

    def put_grounding(self, vehicle_id: str, action_id: str,
                      fleet_id: str | None = None) -> None:
        """Insert an open grounding row directly (simulates a probe car grounded by U4).

        Used by the cleanup-only test to set up a grounded car without running U4.
        """
        import datetime as _dt  # noqa: PLC0415
        fleet_id = fleet_id or RENTAL
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._res().Table(GROUNDINGS).put_item(Item={
            "vehicleId": vehicle_id,
            "actionId": action_id,
            "openFleetId": fleet_id,
            "groundedAt": now,
        })

    def build_stream_event(self, finding: dict) -> dict:
        """Build the DynamoDB Streams event the auto-approver receives (one MODIFY record)."""
        from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415
        ts = TypeSerializer()

        def ddbify(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {"M": {k: ddbify(v) for k, v in obj.items()}}
            if isinstance(obj, list):
                return {"L": [ddbify(x) for x in obj]}
            if isinstance(obj, bool):
                return {"BOOL": obj}
            if isinstance(obj, str):
                return {"S": obj}
            if isinstance(obj, (int, float)):
                return {"N": str(obj)}
            if obj is None:
                return {"NULL": True}
            # Decimal from DynamoDB resource scan
            return {"N": str(obj)}

        new_image = {k: ddbify(v) for k, v in json.loads(json.dumps(finding, default=str)).items()}
        return {
            "Records": [{
                "eventName": "MODIFY",
                "dynamodb": {"SequenceNumber": "0000000000001", "NewImage": new_image},
            }]
        }

    def invoke_approver(self, finding: dict) -> dict:
        """Invoke the auto-approver with one Finding (synchronous; returns the handler result)."""
        import boto3  # noqa: PLC0415
        lam = boto3.client("lambda", region_name=REGION)
        event = self.build_stream_event(finding)
        resp = lam.invoke(FunctionName=AUTO_APPROVER_FN,
                          Payload=json.dumps(event).encode())
        return json.loads(resp["Payload"].read())
