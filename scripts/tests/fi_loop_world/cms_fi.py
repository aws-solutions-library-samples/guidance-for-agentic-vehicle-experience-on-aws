"""CMS FI groundings piece — wraps the real fleet_intelligence/index.py handler.

Covers the two routes the loop script exercises:

  POST /api/v1/fleet-intelligence/groundings
      Body: {vehicleId, campaignId, findingId}
      Group: platform-admin or fleet-operator.

  POST /api/v1/fleet-intelligence/groundings/{vehicleId}/return-to-service
      Group: platform-admin or fleet-operator.
      Returns 200 when the vehicle has a repaired fault grounding, 409 otherwise.

The real handler (``services/fleet_intelligence/index.py``) uses lazy boto3
imports and reads its table names from env vars; both are compatible with moto.

Recall-cache data for the return-to-service check (``blocking_recalls``) is fed
by seeding a fake lifecycle_cache view in the CMS cache table
(``cms-staging-storage-lifecycle-cache``).  The fi_rw_world already creates this
table via ``SHAPES["tables"]``; the loop world writes a fake recall-view item
with ``available: true`` and an empty blocking-recalls list so the RTS route
can proceed (the vehicle has no open recalls).

Environment variables required:
    VEHICLES_TABLE_NAME   = cms-staging-storage-vehicles
    GROUNDINGS_TABLE_NAME = cms-staging-storage-vehicle-groundings
    CMS_VEHICLES_TABLE    (alias used by some sub-modules) same value
    (lifecycle_cache reads LIFECYCLE_CACHE_TABLE_NAME if set; if not, it uses
     a module-level default.  FiLoopWorld sets it explicitly.)

Source:
    CMS main @ 81a90d385 (loaded via git archive — W11 fix)
    services/fleet_intelligence/index.py  (handler)
    services/fleet_intelligence/_auth.py  (group-scope)
    services/fleet_intelligence/groundings.py  (return_to_service)
    services/fleet_intelligence/lifecycle_cache.py  (recall cache read)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_CMS_ROOT = Path(os.environ.get(
    "FI_LOOP_WORLD_CMS_REPO",
    str(Path.home() / "connected-mobility-guidance-on-aws")
))

#: Commit this world piece was authored against (CMS main).
CMS_COMMIT = "81a90d385"

STAGE = "staging"
REGION = "us-west-2"

VEHICLES_TABLE = f"cms-{STAGE}-storage-vehicles"
GROUNDINGS_TABLE = f"cms-{STAGE}-storage-vehicle-groundings"
LIFECYCLE_CACHE_TABLE = f"cms-{STAGE}-storage-lifecycle-cache"

#: S3 bucket and prefix for the CMS lifecycle/recall cache views.
#: Source: live Lambda env ATHENA_OUTPUT_LOC = s3://<bucket>/<prefix>/ (2026-10-21, scrubbed).
CMS_ATHENA_BUCKET = f"cms-{STAGE}-athena-results-123456789012-us-east-1"
CMS_ATHENA_PREFIX = "fleet-intelligence/"

FLEET_INTELLIGENCE_ROOT = str(_CMS_ROOT / "services" / "fleet_intelligence")


def ensure_path() -> None:
    """Add CMS service path so ``services/fleet_intelligence`` imports resolve.

    W11 fix (original): instead of adding the working tree (which is at a later
    commit), extract CMS at CMS_COMMIT (81a90d385) via ``git archive`` into a
    per-test temp dir and add THAT to sys.path.  This pins the exact code the
    brief names.

    W12 fix (CMS half): ``git archive`` failure is now fatal — no silent fallback
    to the working tree, which may be at a different commit and would give different
    results without warning.  If the CMS repo is absent or the commit is not found,
    the test fails immediately with a descriptive message.

    Idempotent: if the extracted path is already on sys.path, returns immediately.

    Cleanup: the copy is deleted when the process exits, including when the
    extraction fails, and ``archive.tar`` is deleted as soon as it is unpacked.
    Before this, every process that called this function left one ~42 MB copy
    in the temp folder; they reached 35 GB and filled the disk on 2026-10-24
    (issues/2026-10-24-cms-fi-temp-copies-fill-disk/). A process that is killed
    (SIGKILL, SIGTERM, a tool timeout) still leaves its copy behind.
    Guarded by scripts/tests/test_fi_loop_world_cms_fi_cleanup.py.
    """
    import atexit      # noqa: PLC0415
    import shutil      # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile    # noqa: PLC0415

    # Use a module-level cache so we only extract once per process.
    if not hasattr(ensure_path, "_extracted_root"):
        ensure_path._extracted_root = None  # type: ignore[attr-defined]

    if ensure_path._extracted_root is not None:
        extract_root = ensure_path._extracted_root
    else:
        if not _CMS_ROOT.exists():
            raise RuntimeError(
                f"CMS repo not found at {_CMS_ROOT}; set FI_LOOP_WORLD_CMS_REPO or "
                f"ensure connected-mobility-guidance-on-aws is present (W12 fix)"
            )
        tmpdir = tempfile.mkdtemp(prefix=f"cms_fi_{CMS_COMMIT[:8]}_")
        # Registered before anything can fail, so a failed archive or extract
        # is cleaned up too.
        atexit.register(shutil.rmtree, tmpdir, ignore_errors=True)
        try:
            with open(tmpdir + "/archive.tar", "wb") as archive_fh:
                subprocess.run(
                    ["git", "-C", str(_CMS_ROOT), "archive", CMS_COMMIT,
                     "--", "services/", "deployment/"],
                    capture_output=False, check=True,
                    stdout=archive_fh,
                )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"git archive {CMS_COMMIT!r} from {_CMS_ROOT} failed (exit "
                f"{exc.returncode}); the CMS commit must be present in the local repo "
                f"(W12 fix: no working-tree fallback)"
            ) from exc
        import tarfile  # noqa: PLC0415
        with tarfile.open(tmpdir + "/archive.tar") as tf:
            tf.extractall(tmpdir)
        os.remove(tmpdir + "/archive.tar")  # half the copy; nothing reads it again
        extract_root = tmpdir
        ensure_path._extracted_root = extract_root  # type: ignore[attr-defined]

    services_root = extract_root + "/services"
    fi_root = extract_root + "/services/fleet_intelligence"
    cms_root = str(_CMS_ROOT)
    for p in (services_root, fi_root, cms_root):
        if p not in sys.path:
            sys.path.append(p)


def make_lambda_env() -> dict[str, str]:
    """Environment variables the CMS FI handler reads."""
    return {
        "VEHICLES_TABLE_NAME": VEHICLES_TABLE,
        "GROUNDINGS_TABLE_NAME": GROUNDINGS_TABLE,
        "LIFECYCLE_CACHE_TABLE_NAME": LIFECYCLE_CACHE_TABLE,
        "FLEET_LOCATIONS_TABLE_NAME": f"cms-{STAGE}-storage-fleet-locations",
        "AVX_ACTIONS_TABLE_NAME": "vsa-staging-avx-actions",
        "COST_ACTIONS_TABLE_NAME": "",
        "VEHICLE_MOVES_TABLE_NAME": "",
        "DEPLOYMENT_STAGE": STAGE,
    }


def api_gateway_event(
    method: str,
    resource: str,
    path: str,
    body: Any = None,
    path_params: dict[str, str] | None = None,
    qs: dict[str, str] | None = None,
    claims: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the CMS FI handler."""
    return {
        "httpMethod": method,
        "resource": resource,
        "path": path,
        "pathParameters": path_params or {},
        "queryStringParameters": qs or {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {"claims": claims or {}}
        },
        "headers": {"Content-Type": "application/json"},
    }


def seed_recall_cache_s3(
    s3_client: Any,
    *,
    bucket: str,
    s3_prefix: str,
    fleet_id: str,
    vin_list: list[str],
    computed_at: str,
    campaign_id: str | None = None,
) -> None:
    """Write recall cache manifest + view objects in moto S3.

    Follows lifecycle_cache.py's S3 layout (CMS main 81a90d385):
      prefix + "_cache/fi-recall-views-v1/manifest.json"
      prefix + "_cache/fi-recall-views-v1/{gen}/recall/{scope_name}.json.gz"

    ``computed_at`` must be a UTC-aware ISO-8601 string.

    When ``campaign_id`` is given, the view includes it in ``campaigns`` and in
    ``vehicle_status`` (one row per VIN in ``vin_list``), so
    ``recall_source.get_fleet_summary`` returns it.  When ``campaign_id`` is None
    the campaigns list is empty and ``blocking_recalls`` returns ``[]``
    (original behaviour, used by the ground/RTS part where no recall is open).

    Writes both:
    - A fleet-scoped view (for GET /recalls?fleetId=...) keyed by ``fleet_id``.
    - Per-VIN vin-shard views (for return-to-service and GET /vehicles/{vin}/recalls)
      keyed by ``vin-shard-{shard:02d}``.  The vin-shard view carries the same
      content but with ``scope = {"shard": N}`` so ``load_view``'s scope check passes.
    """
    import gzip
    import hashlib
    import json as _json

    # Match lifecycle_cache._GENERATION_RE = r"\d{8}T\d{12}Z-[0-9a-f]{8}"
    gen = "20261021T000000000000Z-00000001"
    recall_prefix = f"{s3_prefix}_cache/fi-recall-views-v1/"

    scope_hash = hashlib.sha256(fleet_id.encode("utf-8")).hexdigest()
    scope_name_val = f"fleet-{scope_hash}"
    scope = {"fleetId": fleet_id}

    manifest = {
        "schemaVersion": 2,
        "generation": gen,
        "computedAt": computed_at,
        "windowMonths": 0,
        "seriesShards": 16,
        "kinds": ["recall"],
        "fleets": [fleet_id],
    }
    s3_client.put_object(
        Bucket=bucket,
        Key=f"{recall_prefix}manifest.json",
        Body=_json.dumps(manifest).encode("utf-8"),
    )

    cohort = [{"vin": v} for v in vin_list]
    if campaign_id:
        # Seed a real campaign row + per-VIN vehicle_status rows so
        # recall_source.get_fleet_summary and get_vehicle_recalls both find it.
        campaigns = [{
            "campaign_id": campaign_id,
            "recall_class": "safety",
            "do_not_drive": False,
            "park_outside": False,
            "remedy_available_from": "2027-01-01",
            "received_date": "2026-10-21",
            "component": "Brake actuator",
            "safety_text": "test safety text",
        }]
        vehicle_status = [
            {"vin": v, "campaign_id": campaign_id,
             "remedied": False, "match_basis": "lot",
             "fleet_id": fleet_id}
            for v in vin_list
        ]
        fleet_summary = [{"campaign_id": campaign_id, "affected": len(vin_list),
                          "remedied": 0}]
    else:
        campaigns = []
        # vehicle_status rows with remedied=True so no blocking
        vehicle_status = [{"vin": v, "campaign_id": "NONE", "remedied": True}
                          for v in vin_list]
        fleet_summary = []

    view_body = {
        "schemaVersion": 2,
        "kind": "recall",
        "generation": gen,
        "windowMonths": 0,
        "scope": scope,
        # Use snake_case "computed_at" — recall_source.load_recall_cache reads
        # cache.get("computed_at"), not "computedAt".
        "computed_at": computed_at,
        "computedAt": computed_at,  # also camelCase for lifecycle_cache.load_view compat
        "cohort": cohort,
        "campaigns": campaigns,
        "vehicle_status": vehicle_status,
        "fleet_summary": fleet_summary,
    }
    view_key = f"{recall_prefix}{gen}/recall/{scope_name_val}.json.gz"
    s3_client.put_object(
        Bucket=bucket,
        Key=view_key,
        Body=gzip.compress(_json.dumps(view_body).encode("utf-8")),
    )

    # Write vin-shard views for each VIN so ``_load_recall_cache(vin=vin)`` in the
    # return-to-service route can find the data.
    # lifecycle_cache._recall_slice(vin=vin) computes shard = sha256(vin)[:4] % 16
    # and reads vin-shard-{shard:02d}.json.gz with scope = {"shard": N}.
    # Group VINs by their shard so each shard view carries all VINs in it.
    shards: dict[int, list[str]] = {}
    for vin in vin_list:
        shard = int.from_bytes(hashlib.sha256(vin.encode("utf-8")).digest()[:4], "big") % 16
        shards.setdefault(shard, []).append(vin)

    for shard, shard_vins in shards.items():
        vin_scope = {"shard": shard}
        vin_cohort = [{"vin": v} for v in shard_vins]
        if campaign_id:
            vin_vehicle_status = [
                {"vin": v, "campaign_id": campaign_id,
                 "remedied": False, "match_basis": "lot",
                 "fleet_id": fleet_id}
                for v in shard_vins
            ]
        else:
            vin_vehicle_status = [
                {"vin": v, "campaign_id": "NONE", "remedied": True}
                for v in shard_vins
            ]
        vin_view_body = {
            "schemaVersion": 2,
            "kind": "recall",
            "generation": gen,
            "windowMonths": 0,
            "scope": vin_scope,
            # snake_case for recall_source.load_recall_cache + camelCase for lifecycle_cache
            "computed_at": computed_at,
            "computedAt": computed_at,
            "cohort": vin_cohort,
            "campaigns": campaigns,
            "vehicle_status": vin_vehicle_status,
            "fleet_summary": fleet_summary,
        }
        vin_shard_key = f"{recall_prefix}{gen}/recall/vin-shard-{shard:02d}.json.gz"
        s3_client.put_object(
            Bucket=bucket,
            Key=vin_shard_key,
            Body=gzip.compress(_json.dumps(vin_view_body).encode("utf-8")),
        )

