"""CmsWorld — extends fi_rw_world.World with the CMS FI Lambda and its stage D tables.

Tables added (key schemas from cms:deployment/stacks/storage_stack.py:1ce93a75b):
  cms-staging-storage-pm-plans           PK fleetId (S), SK planId (S)
  cms-staging-storage-vehicle-groundings PK vehicleId (S), SK actionId (S)
  cms-staging-storage-dtc-history        PK vehicleId (S), SK timestamp (N)

FI Lambda environment from cms:deployment/stacks/ui_stack.py:3824-3920 at 1ce93a75b.

W2 additions:
  - AdpFaults installs fleet-health ADP views on world.athena DuckDB.
  - cms-staging-analytics workgroup added to FakeAthena.
  - S3 bucket for ATHENA_OUTPUT_LOC created (health-cache writes).
  - refresh_health(): invoke {"fleetIntelligenceTask": "refresh-health-cache"}.
  - refresh_recall(): invoke {"fleetIntelligenceTask": "refresh-recall-cache"}.
  - Clock pump fires refresh_health() at minute :47 (once per :47 crossing).

Commit 2 additions:
  - _CMS_FAKE_HOST / _patch_cms_requests: intercept requests.post to the fake https
    CMS endpoint and proxy to cms_fi_url. Groundings are recorded in self.groundings.
  - _invoke_api override applies the patch for the findings handler.
  - open() wires VSA_CMS_API_ENDPOINT in _api_env["findings"] to the fake host.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json as _json
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from scripts.tests.fi_rw_world.world import DMS_ROOT, STAGE, World, _py, seed  # type: ignore[attr-defined]

#: DMS commit the world was tested against.  CmsWorld.open refuses any other checkout.
DMS_COMMIT = "de6c181"


def _require_pinned_checkout(path: Path, commit: str) -> None:
    """Raise RuntimeError unless *path* is a clean git repo at commit *commit*."""
    import subprocess  # noqa: PLC0415
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                       capture_output=True, text=True, check=True)
    head = r.stdout.strip()
    if not head.startswith(commit):
        raise RuntimeError(
            f"DMS checkout at {path} must be at {commit!r} but HEAD is {head[:12]!r}"
        )
    s = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                       capture_output=True, text=True, check=True)
    if s.stdout.strip():
        raise RuntimeError(
            f"DMS checkout at {path} is not clean:\n{s.stdout[:400]}"
        )
# ---------------------------------------------------------------------------
# Guard order: the live-check harness's op allowlist must run before the world's router.
# ---------------------------------------------------------------------------
# fi_rw_world's router answers every routed AWS call from a `before-call` handler it
# registers FIRST, and botocore stops at the first handler that returns a response. The
# harness's `_client` registers its op-allowlist guard on the same event afterwards, so
# in the world that guard never ran: a script calling an op its allowlist lacks passed
# every test and failed live (2026-10-23 00:39Z: fleet `pre` alone, ListUsers). While a
# CmsWorld is open, each new client gets the router moved to botocore's LAST bucket, so
# any guard registered on the client runs first, as it does against the real service.
_GUARD_ORDER: dict[str, bool] = {"on": False, "installed": False}


def _install_guard_order() -> None:
    import botocore.session  # noqa: PLC0415
    from scripts.tests.fi_rw_world import awsfake  # noqa: PLC0415
    if _GUARD_ORDER["installed"]:
        return
    inner = botocore.session.Session.create_client

    def create_client(sess: Any, *a: Any, **kw: Any) -> Any:
        client = inner(sess, *a, **kw)
        if _GUARD_ORDER["on"]:
            ev = client.meta.events
            ev.unregister("before-call.*.*", unique_id="fi-rw-world-router")
            ev.register_last("before-call.*.*", awsfake._dispatch,
                             unique_id="fi-cms-world-router-last")
        return client

    botocore.session.Session.create_client = create_client  # type: ignore[method-assign]
    _GUARD_ORDER["installed"] = True


from scripts.tests.fi_cms_world.adp_faults import AdpFaults
from scripts.tests.fi_cms_world.cms_fi import CmsFiApi, extract_cms_archive
from scripts.tests.fi_cms_world import maker as _maker

#: Fake https host used as VSA_CMS_API_ENDPOINT in the findings handler env.
#: Requests to this host are proxied to cms_fi_url by _patch_cms_requests.
_CMS_FAKE_HOST = "cms-fi.world.test"
_CMS_FAKE_ENDPOINT = f"https://{_CMS_FAKE_HOST}/"

PM_PLANS_TABLE = f"cms-{STAGE}-storage-pm-plans"
GROUNDINGS_TABLE = f"cms-{STAGE}-storage-vehicle-groundings"
DTC_HISTORY_TABLE = f"cms-{STAGE}-storage-dtc-history"


def _fi_env(stage: str = STAGE) -> dict[str, str]:
    """The FI Lambda's live env (``data/fi_env.json``, captured read-only, account scrubbed).

    ``AVX_API_ENDPOINT`` keeps an https placeholder host: CMS's ``maker.avx_base_url`` refuses
    anything but https, so the world routes that host to its fake AVX API (stage W3).
    """
    import json
    data = json.loads((Path(__file__).parent / "data" / "fi_env.json").read_text())
    env = dict(data["function"]["Env"])
    assert env["ADP_STAGE"] == stage, env["ADP_STAGE"]
    return env


def _table_def(name: str, pk: str, sk: str | None = None,
               sk_type: str = "S") -> dict[str, Any]:
    keys: list[dict[str, str]] = [{"AttributeName": pk, "KeyType": "HASH"}]
    attrs: list[dict[str, str]] = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        keys.append({"AttributeName": sk, "KeyType": "RANGE"})
        attrs.append({"AttributeName": sk, "AttributeType": sk_type})
    return {
        "TableName": name, "KeySchema": keys,
        "AttributeDefinitions": attrs, "BillingMode": "PAY_PER_REQUEST",
    }


class CmsWorld(World):
    """World adding the CMS FI Lambda (cms_fi_url), stage D tables, and helpers."""

    def open(self) -> None:
        _require_pinned_checkout(DMS_ROOT, DMS_COMMIT)
        # Initialize before super().open() because _refresh_lake is called during
        # _seed_data -> _create_lake_tables (base class) and our override needs the log.
        self._ro_image_log: list[dict[str, Any]] = []
        super().open()
        _install_guard_order()
        _GUARD_ORDER["on"] = True
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="fi-cms-world-", dir="/tmp"
        )
        src_root = extract_cms_archive(self._tmpdir.name)
        self._create_cms_tables()
        self._fi_env = _fi_env()
        self.cms_fi = CmsFiApi(self.pool, self._fi_env, src_root, avx_url=self.avx_url)
        self.cms_fi_url = self.cms_fi.api.url
        # W2: add CMS Athena workgroup and create the health-cache S3 bucket
        self.athena.workgroups.add(self._fi_env.get("ATHENA_WORKGROUP", "cms-staging-analytics"))
        loc = self._fi_env.get("ATHENA_OUTPUT_LOC", "")
        if loc.startswith("s3://"):
            bucket = loc[len("s3://"):].split("/")[0]
            import boto3
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        # W2: install ADP fleet-health views on the world's DuckDB
        self.adp_faults = AdpFaults(self.athena.con, stage=STAGE,
                                    today=self.today)
        self.adp_faults.load_vin_cms_fleet(seed.VIN_FLEET, seed.vin)
        self.adp_faults.load_vehicle_identity(seed.VIN_FLEET, seed.vin)
        self._load_cms_vehicles()
        # W2: health refresh pump at :47 each hour
        self._last_health_refresh: _dt.datetime | None = None
        self.clock.on_advance(self._pump_health_refresh)
        # W3: population Athena tables + ota-campaigns-issued DDB + VIN bucket
        _maker.wire_population(self)
        # W3: investigations DDB/SQS + ADP writer + FiQualityBedrock + routes
        _maker.wire_investigations(self)
        # Commit 2: point the findings handler at the fake CMS https endpoint;
        # groundings intercepted by _patch_cms_requests are recorded here.
        self._api_env["findings"]["VSA_CMS_API_ENDPOINT"] = _CMS_FAKE_ENDPOINT
        self.groundings: list[dict[str, Any]] = []

    def close(self) -> None:
        _GUARD_ORDER["on"] = False
        if hasattr(self, "cms_fi"):
            self.cms_fi.close()
        if hasattr(self, "_tmpdir"):
            src = str(Path(self._tmpdir.name))
            try:
                sys.path.remove(src)
            except ValueError:
                pass
            self._tmpdir.cleanup()
        super().close()

    def _create_cms_tables(self) -> None:
        import botocore.exceptions  # noqa: PLC0415
        ddb = self.internal("dynamodb")
        # PM plans and live-tier tables — only created here, no pre-existing copy to drop.
        for defn in [
            _table_def(PM_PLANS_TABLE, "fleetId", "planId"),
            # The live-tier roster (CMS deployment/scripts/live_tier_roster.py; key read
            # live 2026-10-21: planDate HASH, sk RANGE).
            _table_def(f"cms-{STAGE}-storage-live-plan", "planDate", "sk"),
        ]:
            ddb.create_table(**defn)
        # Groundings table (storage_stack.py 1ce93a75b:1795-1876):
        #   pk vehicleId (S), sk actionId (S)
        #   GSI open-groundings-index: pk openFleetId (S), sk groundedAt (S), ALL.
        # Drop and recreate explicitly so that if the base world (groundings_table=True)
        # has already created this table without the GSI, our definition takes effect.
        try:
            ddb.delete_table(TableName=GROUNDINGS_TABLE)
        except botocore.exceptions.ClientError as _e:
            if _e.response["Error"]["Code"] not in ("ResourceNotFoundException",):
                raise
        ddb.create_table(
            TableName=GROUNDINGS_TABLE,
            KeySchema=[
                {"AttributeName": "vehicleId", "KeyType": "HASH"},
                {"AttributeName": "actionId",  "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "vehicleId",   "AttributeType": "S"},
                {"AttributeName": "actionId",    "AttributeType": "S"},
                {"AttributeName": "openFleetId", "AttributeType": "S"},
                {"AttributeName": "groundedAt",  "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "open-groundings-index",
                "KeySchema": [
                    {"AttributeName": "openFleetId", "KeyType": "HASH"},
                    {"AttributeName": "groundedAt",  "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        # DTC-history table (storage_stack.py 1ce93a75b:1333-1341):
        #   pk vehicleId (S), sk timestamp (N)
        #   GSI active-code-index: pk vehicleId (S), sk activeCode (S), ALL.
        #   Sparse: CLEARED rows omit activeCode and therefore do NOT appear in this index.
        ddb.create_table(
            TableName=DTC_HISTORY_TABLE,
            KeySchema=[
                {"AttributeName": "vehicleId",  "KeyType": "HASH"},
                {"AttributeName": "timestamp",  "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "vehicleId",  "AttributeType": "S"},
                {"AttributeName": "timestamp",  "AttributeType": "N"},
                {"AttributeName": "activeCode", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "active-code-index",
                "KeySchema": [
                    {"AttributeName": "vehicleId",  "KeyType": "HASH"},
                    {"AttributeName": "activeCode", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )

    def _load_cms_vehicles(self) -> None:
        """Populate adp_{stage}_cms_ingest.vehicles from the seed fleet.

        Column mapping: glue_merge_job.py _vehicles_projection iceberg_columns @ ADP 895f7b61.
        All seed vehicles are EVs (fuel_type='electric' → powertrain='ev' per the CASE in
        pm_vehicle_status.sql cms_vehicle CTE).
        """
        s = STAGE
        for i, fid in seed.VIN_FLEET.items():
            self.athena.con.execute(
                f"INSERT INTO adp_{s}_cms_ingest.vehicles VALUES (?,?,?,?,?,?)",
                [seed.vehicle_id(i), seed.vin(i), fid, "Azimuth", 2023, "electric"])

    def seed_pm_plan_to_adp(self, fleet_id: str, plan_id: str, task_code: str,
                             basis: str, interval_value: int, *,
                             start_date: str | None = None,
                             applies_to: str | None = None,
                             vehicle_id: str | None = None) -> None:
        """Mirror a PM plan item into adp_{stage}_cms_ingest.pm_plans.

        Column mapping mirrors ADP 895f7b61
        platform-foundation/source/optional/cms_ingest/glue_merge_job.py
        _pm_plans_projection iceberg_columns:
          interval_value — DDB attr 'interval' (N→INT; renamed from INTERVAL keyword)
          applies_to     — DDB attr 'appliesTo.M' raw DDB-wrapped JSON map string
                           e.g. '{"powertrains":{"L":[{"S":"ev"}]}}' for EV-scoped plans
          vehicle_id     — DDB attr 'vehicleId.S' (NULL for fleet/scoped plans)
          completions    — DDB attr 'completions.L' (NULL; operator completions as raw JSON)
          start_date     — DDB attr 'startDate.S' (calendar next_due when no history)
        """
        s = STAGE
        self.athena.con.execute(
            f"INSERT INTO adp_{s}_cms_ingest.pm_plans "
            "(fleet_id,plan_id,task_code,basis,interval_value,"
            "applies_to,vehicle_id,completions,start_date) VALUES (?,?,?,?,?,?,?,?,?)",
            [fleet_id, plan_id, task_code, basis, interval_value,
             applies_to, vehicle_id, None, start_date])

    def seed_pm_plan(self, fleet_id: str, plan_id: str, **extra: Any) -> None:
        """Put a minimal valid PM plan row for *fleet_id*/*plan_id* into the table."""
        import datetime
        item: dict[str, Any] = {
            "fleetId": fleet_id, "planId": plan_id,
            "taskCode": "TIRE_ROTATION", "basis": "calendar",
            "intervalValue": 6, "createdBySub": "world-seed",
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            **extra,
        }
        self.put(PM_PLANS_TABLE, _py(item))

    # -- Commit 2: CMS grounding proxy -----------------------------------------

    @contextlib.contextmanager
    def _patch_cms_requests(self) -> Iterator[None]:
        """Intercept requests.post to _CMS_FAKE_HOST and proxy to cms_fi_url.

        The CMS executor requires an https VSA_CMS_API_ENDPOINT (validated by
        _cms_base_endpoint) but the fake API is http.  This context manager patches
        agents.executors.cms.requests.post so calls to the fake host are forwarded
        via urllib to cms_fi_url, and each grounding POST is recorded in self.groundings.
        Only active while the findings handler is being invoked.
        """
        import agents.executors.cms as _cms  # noqa: PLC0415
        _orig = _cms.requests.post
        cms_base = self.cms_fi_url.rstrip("/")

        def _proxy(url: str, *, json: Any = None, headers: Any = None,
                   timeout: Any = None, **kw: Any) -> Any:
            from urllib.parse import urlsplit  # noqa: PLC0415
            if urlsplit(url).hostname == _CMS_FAKE_HOST:
                path = urlsplit(url).path
                real_url = cms_base + path
                data = _json.dumps(json).encode() if json else None
                req = urllib.request.Request(
                    real_url, data=data, method="POST",
                    headers={**(headers or {}), "Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=timeout or 5) as r:
                        rb = r.read()

                    class _R:
                        status_code = r.status
                    self.groundings.append({"url": url, "body": json,
                                            "status_code": r.status, "response": rb})
                    return _R()
                except urllib.error.HTTPError as e:
                    rb = e.read()

                    class _Re:
                        status_code = e.code
                    self.groundings.append({"url": url, "body": json,
                                            "status_code": e.code, "response": rb})
                    return _Re()
            return _orig(url, json=json, headers=headers, timeout=timeout, **kw)

        _cms.requests.post = _proxy
        try:
            yield
        finally:
            _cms.requests.post = _orig

    def _invoke_api(self, name: str, fn: Any, event: dict[str, Any],
                    ctx: Any) -> dict[str, Any]:
        """Override: wrap findings handler with CMS grounding proxy; log DMS RO writes.

        W-E: every successful DMS RO create (201) or PATCH (200) appends an image to
        _ro_image_log so _refresh_lake produces one lake row per DMS write.  Real-route
        writes in add_ro_image delegate their append here; test-only ROs (no dealer_id)
        still append in add_ro_image's else-branch.
        """
        if name == "findings":
            with self._patch_cms_requests():
                result = super()._invoke_api(name, fn, event, ctx)
        else:
            result = super()._invoke_api(name, fn, event, ctx)
        # W-E: log an image for every successful DMS RO create or PATCH.
        if name in ("dms:repair_orders.create_repair_order",
                    "dms:repair_orders.update_repair_order"):
            sc = int((result or {}).get("statusCode", 0))
            if sc in (200, 201):
                params = event.get("pathParameters") or {}
                ro_id = params.get("roId")
                if ro_id is None:
                    # create_repair_order: ro_id comes from the response body (201).
                    try:
                        ro_id = _json.loads(
                            (result or {}).get("body") or "{}"
                        ).get("ro_id")
                    except Exception:  # noqa: BLE001
                        pass
                if ro_id:
                    self._append_ro_image_from_route(ro_id)
        return result

    def _append_ro_image_from_route(self, ro_id: str) -> None:
        """Append the current DDB state of *ro_id* to _ro_image_log (W-E).

        Called by _invoke_api after each successful DMS RO create/PATCH so the lake
        tracks every write.  Reads the post-write DDB row to capture handler-written
        fields (updated_at, status, lines, pay_types).
        """
        rows = [r for r in self.scan(self.DMS_RO_TABLE) if r.get("ro_id") == ro_id]
        item = rows[0] if rows else {}
        if not item:
            return
        seq = sum(1 for img in self._ro_image_log if img.get("ro_id") == ro_id) + 1
        self._ro_image_log.append({
            "ro_id": ro_id,
            "vehicle_vin": item.get("vehicle_vin", ""),
            "status": item.get("status", ""),
            "lines": item.get("lines") or [],
            "pay_types": item.get("pay_types") or [],
            "updated_at": item.get("updated_at", ""),
            "_seq_no": str(seq),
        })

    def cms_fi_token(self, groups: list[str], fleet_ids: list[str]) -> str:
        """Mint a CMS pool ID token for an ephemeral user with *groups* and *fleet_ids*."""
        import uuid
        sub = str(uuid.uuid4())
        u = {
            "sub": sub,
            "attrs": {
                "email": f"world-{sub[:8]}@example.invalid",
                "custom:fleetIds": ",".join(fleet_ids),
            },
            "groups": set(groups),
            "password": "World1234!",
            "status": "CONFIRMED",
            "created": self.clock.now(),
        }
        self.pool.users[sub] = u
        return self.pool.mint(u)

    # -- Commit 1: health Findings seed ----------------------------------------

    def seed_health_findings(
        self,
        vin: str,
        fault_code: str,
        fleet_id: str,
        *,
        attempt: int = 1,
        computed_at: str | None = None,
    ) -> list[dict]:
        """Write fleet.health.service Findings (booking + grounding) for *vin*/*fault_code*.

        Uses build_all_items (agents.fi_fleet_health.writer, fault_grounding_enabled=True)
        and build_finding (agents.fi_fleet_health.write). Writes directly to the
        vsa-staging-avx-findings table with projected vin/fleet_id GSI attrs.
        Returns the list of Finding dicts written (without the GSI projection attrs).
        """
        import hashlib as _hlib
        from agents.fi_fleet_health.writer import build_all_items  # noqa: PLC0415
        from agents.fi_fleet_health.write import build_finding  # noqa: PLC0415

        # Look up the catalog entry from adp_faults DuckDB.
        rows = self.athena.con.execute(
            f"SELECT code, band, directive_text FROM adp_{STAGE}_fleet_health.fault_codes "
            "WHERE code = ?",
            [fault_code],
        ).fetchall()
        if not rows:
            raise ValueError(f"Fault code {fault_code!r} not in adp_faults catalog")
        code_val, band_val, directive_val = rows[0]
        if band_val != "p0":
            raise ValueError(f"{fault_code} is not p0 (band={band_val!r})")
        catalog_entry = {"code": code_val, "band": band_val, "directive": directive_val}
        catalog_entries = {fault_code: catalog_entry}

        today_iso = self.today.isoformat()
        open_faults = [{"code": fault_code, "band": "p0",
                        "last_seen_at": today_iso, "raised_at": today_iso}]
        if computed_at is None:
            computed_at = self.clock.iso()

        inputs_hash = _hlib.sha256(
            f"{vin}:{fault_code}:{attempt}:{computed_at}".encode()
        ).hexdigest()

        items = build_all_items(
            vin=vin, attempt=attempt, open_faults=open_faults,
            catalog_entries=catalog_entries, agent2_findings=[],
            pm_tasks=[], already_grounded=False, fault_grounding_enabled=True,
            model_reply=None, candidate_dealer_ids=[],
        )
        findings: list[dict] = []
        for item in items:
            f = build_finding(
                vin=vin, fleet_id=fleet_id, item=item, computed_at=computed_at,
                inputs_hash=inputs_hash, evidence=[], confidence="high",
            )
            # Write with top-level GSI projection attrs (vin, fleet_id for by_vin / by_fleet).
            self.put("vsa-staging-avx-findings", {**f, "vin": vin, "fleet_id": fleet_id})
            findings.append(f)
        return findings

    # -- Commit 3: DMS service-advisor token helper ----------------------------
    # Routes POST/PATCH /api/dms/dealers/{dealerId}/repair-orders[/{roId}] are
    # already registered in fi_rw_world._DMS_ROUTES via the base World.
    # This helper mints a token that passes authorize_dealer_scope (custom:dealerIds).

    def dms_sa_token(self, dealer_id: str) -> str:
        """Mint a service-advisor DMS token that grants access to *dealer_id*.

        custom:dealerIds is a comma-separated string; the DMS auth.get_granted_dealer_ids
        accepts both JSON array and CSV formats.
        """
        import uuid as _uuid  # noqa: PLC0415
        sub = str(_uuid.uuid4())
        u = {
            "sub": sub,
            "attrs": {
                "email": f"world-sa-{sub[:8]}@example.invalid",
                "custom:dealerIds": dealer_id,
            },
            "groups": {"service-advisor"},
            "password": "World1234!",
            "status": "CONFIRMED",
            "created": self.clock.now(),
        }
        self.pool.users[sub] = u
        return self.pool.mint(u)

    # -- W2: health and recall refresh ----------------------------------------

    DMS_RO_TABLE = "dms-staging-repair-orders"

    def add_ro_image(self, ro_id: str, vin: str, status: str,
                     labor_op: str, updated_at: str, seq_no: str = "1") -> None:
        """Update RO status without overwriting the real DMS RO.

        B-2 (review-B W2 + review-C W9): for ROs the DMS handler created (have
        ``dealer_id``), PATCH via the real DMS route so that ``lines``,
        ``description``, ``dealer_id`` and all other handler-written fields are
        preserved.  For test-injected ROs that don't exist in DDB (no ``dealer_id``),
        fall back to a minimal ``put_item`` so tests can inject a fake repairRef.

        For real DMS ROs the log append is done by ``_invoke_api`` when the PATCH
        route responds (W-E); for test-only ROs it is done here explicitly.
        """
        # Look up the RO to see whether it is a real handler-created item.
        rows = [r for r in self.scan(self.DMS_RO_TABLE) if r.get("ro_id") == ro_id]
        item = rows[0] if rows else {}

        if item.get("dealer_id"):
            # Real DMS RO: PATCH through the real route — preserves lines, description, etc.
            dealer_id = str(item["dealer_id"])
            token = self.dms_sa_token(dealer_id)
            path = (
                f"/api/dms/dealers/{urllib.parse.quote(dealer_id, safe='')}"
                f"/repair-orders/{urllib.parse.quote(ro_id, safe='')}"
            )
            data = _json.dumps({"status": status}).encode()
            req = urllib.request.Request(
                self.dms_url.rstrip("/") + "/" + path.lstrip("/"),
                method="PATCH",
                data=data,
                headers={"Authorization": token, "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    pb = _json.loads(resp.read() or b"{}")
                    ps = resp.status
            except urllib.error.HTTPError as exc:
                ps, pb = exc.code, _json.loads(exc.read() or b"{}")
            if ps != 200:
                raise RuntimeError(
                    f"add_ro_image: DMS PATCH {ro_id!r} to {status!r} failed {ps}: {pb}"
                )
            # Re-read to capture handler-written updated_at and preserve lines.
            rows2 = [r for r in self.scan(self.DMS_RO_TABLE) if r.get("ro_id") == ro_id]
            item = rows2[0] if rows2 else item
        else:
            # Test-only RO (doesn't exist or has no dealer_id): put_item with minimal fields.
            self.put(self.DMS_RO_TABLE, {
                "ro_id": ro_id, "vehicle_vin": vin, "status": status,
                "lines": [{"labor_op": labor_op}], "updated_at": updated_at,
            })
            item = {
                "ro_id": ro_id, "vehicle_vin": vin, "status": status,
                "lines": [{"labor_op": labor_op}], "updated_at": updated_at,
            }
            # Test-only ROs don't go through the route, so _invoke_api won't log them.
            # Append explicitly here.  Real DMS ROs (dealer_id path above) are logged
            # by _invoke_api after the PATCH route returns (W-E).
            seq = sum(1 for img in self._ro_image_log if img.get("ro_id") == ro_id) + 1
            self._ro_image_log.append({
                "ro_id": ro_id,
                "vehicle_vin": item.get("vehicle_vin", vin),
                "status": item.get("status", status),
                "lines": item.get("lines") or [],
                "pay_types": item.get("pay_types") or [],
                "updated_at": item.get("updated_at", updated_at),
                "_seq_no": str(seq),
            })

    def _refresh_lake(self) -> None:
        """Override: produce one lake image per DMS write (rising _seq_no).

        B-2 (review-C W9): instead of one row per RO from a DDB scan (always
        ``_seq_no "1"``), emit one row per entry in ``_ro_image_log`` for tracked
        ROs, and fall back to a single scan-row for un-tracked ones.  Delegates
        everything else (recall_vehicle_status, warranty_candidates) to super().
        """
        super()._refresh_lake()  # recall_vehicle_status + warranty_candidates + default RO rows

        if not self._ro_image_log:
            return  # nothing tracked; super()'s RO rows are correct

        # Re-build dms_staging.repair_orders using the image log.
        logged_ids = {img["ro_id"] for img in self._ro_image_log}
        # One row per image-log entry (tracked ROs).
        ros_logged = [
            {
                **img,
                "_change_kind": "upsert",
                "lines": _json.dumps(img.get("lines") or [], default=str),
                "pay_types": _json.dumps(img.get("pay_types") or [], default=str),
            }
            for img in self._ro_image_log
        ]
        # One row per un-tracked RO from DDB (falls back to _seq_no "1").
        ros_untracked = [
            {
                **r,
                "_change_kind": "upsert",
                "_seq_no": "1",
                "lines": _json.dumps(r.get("lines") or [], default=str),
                "pay_types": _json.dumps(r.get("pay_types") or [], default=str),
            }
            for r in self.scan(self.DMS_RO_TABLE)
            if r.get("ro_id") not in logged_ids
        ]
        self.athena.create_table(
            "dms_staging.repair_orders",
            self._glue_cols("dms_staging.repair_orders"),
            ros_untracked + ros_logged,
        )

    def _call_fi_task(self, task: str) -> dict[str, Any]:
        """Invoke the CMS FI handler with a refresh task event, under the Lambda env."""
        from scripts.tests.fi_rw_world.lambdas import lambda_env  # noqa: PLC0415
        with lambda_env(self._fi_env, self.cms_fi._fn_name, "us-west-2"):
            return self.cms_fi._mod.handler(
                {"fleetIntelligenceTask": task}, None)  # type: ignore[return-value]

    def refresh_health(self) -> dict[str, Any]:
        """Run the CMS health-cache refresh in-process; writes the S3 faults/pm/findings caches."""
        return self._call_fi_task("refresh-health-cache")

    def refresh_population(self) -> dict[str, Any]:
        """Run the CMS population-cache refresh in-process.

        Reads from adp_{stage}_engineering.{fault_patterns,quality_signals,digital_thread},
        adp_{stage}_ota_campaigns.ota_campaigns, adp_{stage}_cms_ingest.{vin_cms_fleet,fleets},
        and adp-{stage}-ota-campaigns-issued DDB.
        """
        return _maker.refresh_population(self)

    def seed_recall_cache(self) -> None:
        """Write minimal stub recall cache for every seed VIN shard.

        Bypasses the full _refresh_recall_cache() Athena reads (which need
        adp_staging_fleet_recall.recall_fleet_summary etc.) and writes
        gzip-compressed lifecycle_cache views directly, so RTS recall check
        returns [] (no blocking recalls) for all seed VINs.
        """
        import hashlib  # noqa: PLC0415
        from scripts.tests.fi_rw_world.lambdas import lambda_env  # noqa: PLC0415
        shards: dict[int, list[str]] = {}
        for i in seed.VIN_FLEET:
            v = seed.vin(i)
            s = int.from_bytes(hashlib.sha256(v.encode()).digest()[:4], "big") % 16
            shards.setdefault(s, []).append(v)
        with lambda_env(self._fi_env, self.cms_fi._fn_name, "us-west-2"):
            lc = __import__("services.fleet_intelligence.lifecycle_cache", fromlist=[""])
            now = self.clock.now()
            views = [
                (lc.KIND_RECALL, f"vin-shard-{shard:02d}", {"shard": shard}, {
                    "computed_at": now.isoformat(),
                    "cohort": [{"vin": v} for v in vins],
                    "campaigns": [], "vehicle_status": [], "fleet_summary": [],
                })
                for shard, vins in shards.items()
            ]
            lc.publish(0, views, computed_at=now, kinds=[lc.KIND_RECALL],
                       fleets=sorted(set(seed.VIN_FLEET.values())),
                       root=lc.recall_views_root())

    def _pump_health_refresh(self) -> None:
        """Clock pump: fire refresh_health() once when the clock crosses minute :47."""
        t = self.clock.now()
        if t.minute == 47 and (
            self._last_health_refresh is None
            or (t - self._last_health_refresh).total_seconds() > 1800
        ):
            self._last_health_refresh = t
            try:
                self.refresh_health()
            except Exception:  # noqa: BLE001 — test assertions catch failures explicitly
                pass
