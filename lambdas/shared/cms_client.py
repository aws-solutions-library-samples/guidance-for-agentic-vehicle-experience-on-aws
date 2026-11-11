"""CMS client interface.

Production impl talks to CMS Redis LKS, DynamoDB DTC history, and the recall
integration service. The fake (cms_client_fake.py) provides fixture data so the
classifier can be built and tested without a live CMS environment.

Selection: set env var VSA_CMS_CLIENT=fake (default) or =real.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from triage_classifier.types import ActiveDtc, OpenRecall, TelemetrySnapshot


@runtime_checkable
class CmsClient(Protocol):
    def get_telemetry(self, vin: str) -> TelemetrySnapshot: ...
    def get_active_dtcs(self, vin: str) -> tuple[ActiveDtc, ...]: ...
    def get_open_recalls(self, vin: str) -> tuple[OpenRecall, ...]: ...


def get_client() -> CmsClient:
    mode = os.environ.get("VSA_CMS_CLIENT", "fake")
    if mode == "fake":
        from . import cms_client_fake  # local import keeps boto3 out of hot path

        return cms_client_fake.FakeCmsClient()
    if mode == "real":
        from . import cms_client_real

        return cms_client_real.RealCmsClient()
    raise ValueError(f"Unknown VSA_CMS_CLIENT mode: {mode}")
