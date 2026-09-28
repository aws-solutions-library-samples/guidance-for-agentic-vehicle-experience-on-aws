"""Configuration loading for the VSA supervisor.

All runtime config comes from environment variables with sensible defaults
for the Cascadia demo tenant. CLI flags in cli.py can override the ones
that make sense to override per invocation (tenantId, VIN).

The Cognito demo sign-in flow is a dev convenience. Real deployments will
receive a JWT from the caller (iOS app, AgentCore session) and will never
run this flow.

CVX-on-foundation v1 ADP Bedrock KB wiring (spec 2026-06-18-cvx-adp-kb-wireup):
three optional fields in the Config dataclass populate from environment
variables for cross-account KB Retrieve grounding:
  - VSA_ADP_KB_ID: Bedrock KB ID (e.g., YULHRAUMNU); empty string disables
    KB grounding (tools fall back to local corpora).
  - VSA_ADP_REGION: AWS region for the KB (default us-east-1).
  - VSA_ADP_ACCOUNT_ID: AWS account ID for cross-account principal resolution
    (default: resolved via STS get-caller-identity if unset).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError


# Cascadia tenant is the v1 launch tenant. These match what seed-meridian-fleet.py
# and the CDK stacks deployed. See VIRTUAL_SERVICE_AGENT.md §10 locked
# decisions and VSA_TENANT_CONFIG.md §4.
_DEFAULTS = {
    "VSA_API_BASE": "",  # Set after deploying: vsa-{stage}-api stack output
    # De-branded default (2026-08-21). Was a real company name; a default tenant should
    # not name a customer. See lambdas/api-assistant-chat/handler.py for the same change.
    "VSA_TENANT_ID": "vehicle-owner",
    "VSA_COGNITO_POOL_ID": "",  # Set after deploying: tenant pool output
    "VSA_COGNITO_CLIENT_ID": "",  # Set after deploying: tenant pool output
    # Dev sign-in. There is no shared demo password: each seeded account has its own
    # 24-char value in Secrets Manager (`vsa-{stage}-meridian-fleet-demo-passwords`, a JSON
    # map keyed by email) as of the 2026-08-04 rotation — see
    # issues/2026-08-03-cvx-demo-password-rotation/. VSA_DEMO_PASSWORD therefore has no
    # default and must be supplied by the caller; leave it empty here.
    #
    # Note the username below is seeded by scripts/seed-meridian-fleet-user.py, which defaults
    # to STAGE=prod, so it resolves to a PROD-pool account. Against staging, set
    # VSA_DEMO_USERNAME to one of the personas seeded by scripts/seed-persona-users.py.
    "VSA_DEMO_USERNAME": "demo@meridian-fleet.example",
    "VSA_DEMO_PASSWORD": "",
    "VSA_DEFAULT_VIN": "",
    "VSA_DEFAULT_VEHICLE_ID": "",
    "VSA_DEFAULT_DRIVER_ID": "",
    "VSA_KB_FIXTURES_DIR": "corpora",
    "AWS_REGION": "us-east-1",
    "NOVA_SONIC_REGION": "",  # Falls back to AWS_REGION when unset
    "CMS_STAGE": "dev",
    "VSA_SUPERVISOR_MODEL_ID": "us.anthropic.claude-sonnet-4-6",
    "VSA_SUPERVISOR_MAX_TOKENS": "4096",
}

_TOKEN_CACHE_PATH = Path.home() / ".vsa" / "tokens.json"


def env(key: str) -> str:
    """Read an env var, falling back to the defaults table.

    Raises KeyError if neither is set — makes missing config loud instead
    of silently defaulting to None.
    """
    value = os.environ.get(key)
    if value is not None:
        return value
    if key in _DEFAULTS:
        return _DEFAULTS[key]
    raise KeyError(f"Required env var {key} not set and no default")


def _api_base_region(api_base: str) -> str | None:
    """Region named by an AWS API Gateway endpoint, or None if undeterminable.

    Deliberately conservative: returns None for custom domains, localhost, empty
    strings and anything else it cannot read with certainty, so the caller only
    ever compares two regions it actually knows.
    """
    match = re.search(
        r"\.execute-api\.([a-z]{2}(?:-[a-z]+)+-\d)\.amazonaws\.com", api_base or ""
    )
    return match.group(1) if match else None


def assert_api_base_region_matches(api_base: str, aws_region: str) -> None:
    """Fail closed when VSA_API_BASE points at a different region than the deploy.

    On 2026-09-22 a staging deploy baked
    `VSA_API_BASE=https://…execute-api.us-east-1.amazonaws.com/prod` — the CVX
    prod API, torn down 2026-09-02 — into both staging runtimes, because the
    Makefile prefers an ambient value over its own CloudFormation lookup and the
    operator's shell carried it from a stale `.env`. Every `api_base`-derived
    call then failed: a driver asking for a human agent was told escalation was
    unavailable, and auto-triage failed DNS resolution.

    The guard lives HERE, in the runtime's own config, rather than in the deploy
    target, for two reasons:

    1. **It covers every path.** A Makefile guard protects `make deploy-staging`
       and nothing else — not a hand-run `agentcore deploy`, not a hand-edited
       runtime environment, not CI. `--env` replaces the whole runtime
       environment on redeploy, so a wrong value can arrive without the Makefile
       being involved at all.
    2. **A printed value is not a control.** The Makefile already echoed the
       wrong URL twice; it was read past. This raises.

    Cross-region is treated as an error rather than a warning because there is no
    legitimate case for it: `VSA_API_BASE` is CVX's own API, deployed by the same
    stack set, in the same region. Cross-region CMS access has its own separate
    variable (`VSA_CMS_API_BASE`), which this does not touch.

    See issues/2026-09-22-stale-env-vsa-api-base-leaked-into-staging-runtime/.
    """
    base_region = _api_base_region(api_base)
    if base_region is None or not aws_region or base_region == aws_region:
        return
    raise ValueError(
        f"VSA_API_BASE names region {base_region!r} but AWS_REGION is "
        f"{aws_region!r}:\n"
        f"    VSA_API_BASE={api_base}\n"
        f"There is no valid configuration where CVX's own API lives in a "
        f"different region from the runtime — this is the shape of a stale value "
        f"inherited from a shell or a `.env`, and it silently breaks triage, "
        f"escalation, booking and service history.\n"
        f"Resolve it by unsetting VSA_API_BASE so the deploy resolves it from the "
        f"vsa-{{stage}}-api stack's RestApiUrl output, or by setting it to that "
        f"stack's value for {aws_region}."
    )


@dataclass(frozen=True)
class Config:
    """Static configuration for a supervisor run."""

    api_base: str
    tenant_id: str
    cognito_pool_id: str
    cognito_client_id: str
    demo_username: str
    demo_password: str
    default_vin: str
    default_vehicle_id: str
    default_driver_id: str
    kb_fixtures_dir: Path
    aws_region: str
    nova_sonic_region: str  # Region for Nova Sonic bidirectional stream (may differ from aws_region)
    supervisor_model_id: str
    supervisor_max_tokens: int
    # CVX-on-foundation v1 carry-forward closure (spec 2026-06-18-cvx-adp-kb-wireup).
    # ADP Bedrock KB wiring consumed by the four KB-grounded tool factories
    # (tools/retrieve.py, tools/knowledge.py, tools/parts_lookup.py,
    # tools/diy_repair_advisor.py), which read getattr(config, "adp_kb_id", "").
    # Safe-empty defaults keep existing SimpleNamespace fixtures and v1 callers
    # green; from_env() populates them from VSA_ADP_* env vars.
    adp_kb_id: str = ""
    adp_region: str = "us-east-1"
    # repr=False keeps the resolved account id out of the dataclass repr so a
    # future repr(config) / f"{config!r}" cannot leak it into logs
    # (security-review cycle 1 hardening, spec 2026-06-18-cvx-adp-kb-wireup).
    adp_account_id: str = field(default="", repr=False)
    # CVX Acquire spec 2026-08-04-cvx-oem-discover-order-meridian — Group 2 (merged from predecessor 2026-07-21).
    # Three optional fields follow the same safe-empty-default pattern as the
    # ADP KB fields above. Tools that need Acquire tables read these via
    # getattr(config, "acquire_catalog_table", ""); empty string = not wired.
    # VSA_ACQUIRE_KB_SOURCE_CATEGORY defaults to "vehicle_manufacturing" — the single
    # generic manufacturing corpus. Amendment 1 of spec
    # 2026-08-04-cvx-oem-discover-order-meridian collapsed the two former
    # vehicle-type-specific manufacturing corpora into this one, so there is no
    # per-vehicle-type category to choose between any more.
    acquire_catalog_table: str = ""
    acquire_orders_table: str = ""
    acquire_kb_source_category: str = "vehicle_manufacturing"
    # CVX Discover spec 2026-08-04-cvx-oem-discover-order-meridian — Group 2 (merged from predecessor 2026-07-28).
    # Three optional fields follow the same safe-empty-default pattern.
    # acquire_leads_table:              name of the vsa-<stage>-acquire-leads DDB
    #                                   table; empty string = Discover tools fall
    #                                   back to {available: false}.
    # discover_kb_source_category:      KB source-category filter for Discover-phase
    #                                   grounding turns; default "vehicle_shopping_guide"
    #                                   matches the committed corpus (amendment 2 of spec
    #                                   2026-08-04-cvx-oem-discover-order-meridian).
    # discover_guardrails_identifier:   Bedrock Guardrails identifier threaded into
    #                                   the offers_lookup() narration turn; currently
    #                                   a placeholder (no policy authored in v1).
    acquire_leads_table: str = ""
    discover_kb_source_category: str = "vehicle_shopping_guide"
    discover_guardrails_identifier: str = "discover-offers-guardrails-v1"
    # CVX Fleet Manager persona (spec 2026-08-21-cvx-fleet-manager-persona).
    # Base URL for the CMS fleet API endpoints (fleet-health, daily-briefing,
    # fleet-actions, decision-journal). Distinct from api_base (the CVX-internal
    # API URL) because the deployment may route fleet-aggregate calls to a
    # different host. Empty string = tools degrade gracefully with
    # {"available": false, "reason": "CMS API not configured"}.
    cms_api_base: str = field(default_factory=lambda: os.environ.get("VSA_CMS_API_BASE", ""))
    # CVX text-session memory (spec 2026-09-22-cvx-text-session-memory, D5).
    #
    # memory_id / memory_region: AgentCore Memory resource for conversation turn
    # persistence and persona long-term memory.  Reads from the SAME env vars
    # already consumed by build_persona_loader_config() above, so a single
    # deployment sets them once.  Safe-empty defaults follow the ADP/Acquire/
    # Discover fields above — existing SimpleNamespace test fixtures keep working
    # without setting these vars.
    #
    #   VSA_FOUNDATION_MEMORY_ID  — Memory resource id (e.g. vsa_staging_cvx_foundation_memory-…)
    #   VSA_MEMORY_REGION         — AWS region for that Memory (default us-west-2)
    #
    # These fields fix the AttributeError at 8 bare `config.memory_id` call-sites
    # in tools/, where getattr was NEVER used for memory_id (unlike memory_region).
    # Adding them here ends the silent failure without converting the call-sites to
    # getattr — a getattr default would make the degradation uniform instead of
    # ending it (spec D5).
    memory_id: str = ""
    memory_region: str = "us-west-2"
    # repr= policy note: memory_id above uses repr=True (the dataclass default),
    # while adp_account_id (below) uses repr=False.  An AWS account ID is a
    # numeric account identifier that appears in ARNs and IAM policy resources,
    # making it a more persistent cross-service identifier than a Memory resource
    # ID (which is scoped to a single AgentCore resource and rotates with the
    # resource).  The asymmetry is intentional: adp_account_id's repr=False was
    # added specifically to keep it out of repr(config) / f"{config!r}" log
    # emission (security-review 2026-06-18-cvx-adp-kb-wireup cycle 1).  If
    # memory_id is later determined to carry equivalent sensitivity, set
    # field(default="", repr=False) here to match.
    # tool_memory_reads: gates the 8 retrieve_memory_records calls in tools/ that
    # become live once memory_id is populated.  Those tools serve the VOICE path,
    # which has a ~1 s per-tool-return budget (Nova Sonic bidi loop).
    #
    # DEFAULT IS **FALSE** — changed 2026-09-22 on the strength of T4.2's measurement,
    # per spec R1 ("accept only with numbers") and T4.2's decision rule ("if it
    # regresses, default the flag off and file the follow-up — do NOT revert
    # hydration, which is independent").
    #
    # Measured against the live staging Memory resource, mirroring
    # tools/vehicle_identity.py's exact call (same namespace shape, same 3
    # metadataFilters, topK=1):
    #
    #     boto3.client("bedrock-agentcore")  warm p50   1.6 ms   (built PER tool call)
    #     retrieve_memory_records            p50  341.5 ms, p95 514.7 ms
    #     ---------------------------------------------------------------
    #     per gated tool call                p50  343.1 ms, p95 516.4 ms
    #
    # Before D5 these calls raised AttributeError immediately, so the added cost is
    # the whole figure, not a delta. One call consumes ~34-52% of the per-call
    # budget; two in a turn cross 1 s at p95 on user-perceived latency. And this is
    # the FLOOR — the measured namespaces returned 0 records, so a populated
    # namespace doing real semantic search can only be slower.
    #
    # Exposure: all three voice personas carry gated tools — consumer 6,
    # fleet_driver 6, retail_advisor 3.
    #
    # Defaulting OFF costs nothing that works today: these 8 call-sites have raised
    # AttributeError on every invocation since fe18930 (2026-06-18) and returned
    # {"available": False, "reason": "<x> not yet loaded"}. Flag off returns that
    # SAME dict without the network call. So off == today's behaviour, minus the
    # latency. Nothing is relying on enrichment that has never once succeeded.
    #
    # Set VSA_TOOL_MEMORY_READS=true to switch the reads ON once someone decides
    # 343 ms of persona enrichment is worth it on the voice path. Text-path session
    # memory (hydration) is INDEPENDENT of this flag and stays on either way.
    #
    # When the flag is off each tool returns its own existing degraded payload
    # WITHOUT constructing a boto3 client and without calling the service — so
    # on-vs-off differ only in latency and in whether the network is touched; the
    # response shape is identical either way. See the gate in
    # tools/vehicle_identity.py and tests/test_tool_memory_reads_gate.py (T2.5).
    #
    # NOTE ON THE TWO DEFAULTS, which deliberately differ: the 8 tools read this via
    # getattr(config, "tool_memory_reads", True) — default TRUE — because they also
    # accept a PersonaLoaderConfig / SimpleNamespace that lacks the field, and every
    # existing test fixture is one of those. That getattr default governs TEST
    # fixtures; the dataclass default below governs PRODUCTION, where every non-test
    # path passes Config.from_env() (verified: bidi_app.py:525 builds it, :709 hands
    # it to the factories). Neither is in tension with D5's no-getattr rule, which
    # forbids a getattr default for memory_id where it would mask a misconfiguration.
    tool_memory_reads: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        repo_root = Path(__file__).resolve().parents[2]
        fixtures = repo_root / env("VSA_KB_FIXTURES_DIR")
        # ADP Bedrock KB wiring (spec 2026-06-18-cvx-adp-kb-wireup). The KB id
        # and region come straight from env; the account id falls back to STS
        # get-caller-identity (mirrors build_persona_loader_config) so we never
        # inline a literal account id. The fallback is silent ONLY for expected
        # AWS credential/endpoint errors so unit-test and unconfigured contexts
        # can still build Config.from_env(); unexpected errors propagate (see
        # the narrowed except below) so a real bug fails loud at root cause.
        adp_kb_id = os.environ.get("VSA_ADP_KB_ID", "")
        adp_region = os.environ.get("VSA_ADP_REGION", "us-east-1")
        adp_account_id = os.environ.get("VSA_ADP_ACCOUNT_ID", "")
        if not adp_account_id:
            try:
                adp_account_id = boto3.client("sts").get_caller_identity()["Account"]
            except (BotoCoreError, ClientError):
                # No creds, IMDS throttle, unreachable endpoint, expired token —
                # all expected in test/unconfigured contexts -> safe-empty.
                # A non-botocore exception is NOT swallowed (security-review
                # cycle 1 hardening): it surfaces here rather than failing
                # silently in downstream ARN construction.
                adp_account_id = ""
        # CVX Acquire env vars (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-21).
        acquire_catalog_table = os.environ.get("VSA_ACQUIRE_CATALOG_TABLE", "")
        acquire_orders_table = os.environ.get("VSA_ACQUIRE_ORDERS_TABLE", "")
        acquire_kb_source_category = os.environ.get("VSA_ACQUIRE_KB_SOURCE_CATEGORY", "vehicle_manufacturing")
        # CVX Discover env vars (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-28).
        acquire_leads_table = os.environ.get("VSA_ACQUIRE_LEADS_TABLE", "")
        discover_kb_source_category = os.environ.get("VSA_DISCOVER_KB_SOURCE_CATEGORY", "vehicle_shopping_guide")
        discover_guardrails_identifier = os.environ.get("VSA_DISCOVER_GUARDRAILS_ID", "discover-offers-guardrails-v1")
        # CVX Fleet Manager env var (spec 2026-08-21-cvx-fleet-manager-persona).
        cms_api_base = os.environ.get("VSA_CMS_API_BASE", "")
        # CVX text-session memory env vars (spec 2026-09-22-cvx-text-session-memory, D5).
        # Both vars are already read by build_persona_loader_config(); reading them here
        # makes them available on Config so the 8 tool call-sites stop raising
        # AttributeError.  Safe-empty defaults keep unconfigured and test contexts working.
        # NOTE: any deployment where VSA_MEMORY_REGION differs from "us-west-2" will now
        # see a REAL behaviour change: tools were always falling back to the getattr default
        # "us-west-2" before this commit; they now read the actual env var.  If your env
        # sets VSA_MEMORY_REGION to a different region, the tool clients will target that
        # region for the first time.  This is the intended fix — the getattr fallback was
        # masking the env var — but flag it in the handback as a live behaviour change.
        memory_id = os.environ.get("VSA_FOUNDATION_MEMORY_ID", "")
        memory_region = os.environ.get("VSA_MEMORY_REGION", "us-west-2")
        # VSA_TOOL_MEMORY_READS defaults on; any falsy string ("0", "false", "off", "no",
        # "") turns it off so a voice latency regression can be isolated (spec D5 / R1).
        # Default "false" per T4.2's measurement — see the field comment above for
        # the numbers. Only an explicit truthy value switches the 8 tool memory
        # reads on; absent/blank/falsy all leave them gated, which is today's
        # behaviour minus ~343 ms per call.
        _tmr_raw = os.environ.get("VSA_TOOL_MEMORY_READS", "false").strip().lower()
        tool_memory_reads = _tmr_raw in ("1", "true", "on", "yes")
        # Resolved before the constructor call so the cross-region check can see
        # both values. See assert_api_base_region_matches for why this is a hard
        # failure and why it belongs here rather than in the deploy target.
        api_base = env("VSA_API_BASE").rstrip("/")
        aws_region = env("AWS_REGION")
        assert_api_base_region_matches(api_base, aws_region)
        return cls(
            api_base=api_base,
            tenant_id=env("VSA_TENANT_ID"),
            cognito_pool_id=env("VSA_COGNITO_POOL_ID"),
            cognito_client_id=env("VSA_COGNITO_CLIENT_ID"),
            demo_username=env("VSA_DEMO_USERNAME"),
            demo_password=env("VSA_DEMO_PASSWORD"),
            default_vin=env("VSA_DEFAULT_VIN"),
            default_vehicle_id=env("VSA_DEFAULT_VEHICLE_ID"),
            default_driver_id=env("VSA_DEFAULT_DRIVER_ID"),
            kb_fixtures_dir=fixtures,
            aws_region=aws_region,
            nova_sonic_region=os.environ.get("NOVA_SONIC_REGION") or aws_region,
            supervisor_model_id=env("VSA_SUPERVISOR_MODEL_ID"),
            supervisor_max_tokens=int(env("VSA_SUPERVISOR_MAX_TOKENS")),
            adp_kb_id=adp_kb_id,
            adp_region=adp_region,
            adp_account_id=adp_account_id,
            acquire_catalog_table=acquire_catalog_table,
            acquire_orders_table=acquire_orders_table,
            acquire_kb_source_category=acquire_kb_source_category,
            acquire_leads_table=acquire_leads_table,
            discover_kb_source_category=discover_kb_source_category,
            discover_guardrails_identifier=discover_guardrails_identifier,
            cms_api_base=cms_api_base,
            memory_id=memory_id,
            memory_region=memory_region,
            tool_memory_reads=tool_memory_reads,
        )


def _jwt_valid(token: str, leeway: int = 60) -> bool:
    """Return True if the JWT exp claim is in the future (with leeway seconds)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        exp = json.loads(base64.b64decode(payload)).get("exp")
        return exp is not None and time.time() < exp - leeway
    except Exception:  # noqa: BLE001
        return False


def get_cached_id_token(config: Config) -> str:
    """Return a cached Cognito IdToken for the demo user, refreshing if missing.

    The cache file is a simple JSON dict keyed by tenant. For Phase 3 local
    testing this is adequate. Production JWT handling lives elsewhere.

    This is NOT how the iOS app or AgentCore will get a JWT — they'll have
    their own sign-in flows. This is a dev convenience only.
    """
    cache = _load_cache()
    entry = cache.get(config.tenant_id, {})
    id_token = entry.get("idToken")
    if id_token and _jwt_valid(id_token):
        return id_token

    # No cached token; sign in fresh. Cognito USER_PASSWORD_AUTH flow.
    client = boto3.client("cognito-idp", region_name=config.aws_region)
    resp = client.initiate_auth(
        ClientId=config.cognito_client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": config.demo_username,
            "PASSWORD": config.demo_password,
        },
    )
    auth = resp["AuthenticationResult"]
    id_token = auth["IdToken"]
    cache[config.tenant_id] = {
        "idToken": id_token,
        "accessToken": auth["AccessToken"],
        "refreshToken": auth.get("RefreshToken"),
    }
    _save_cache(cache)
    return id_token


def clear_cached_tokens() -> None:
    """Wipe the token cache. Used when auth fails (likely expired token)."""
    if _TOKEN_CACHE_PATH.exists():
        _TOKEN_CACHE_PATH.unlink()


def _load_cache() -> dict:
    if not _TOKEN_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_TOKEN_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    # 0600 — tokens are sensitive.
    _TOKEN_CACHE_PATH.chmod(0o600)


def build_persona_loader_config():  # -> PersonaLoaderConfig
    """Build a PersonaLoaderConfig from environment variables.

    Only VSA_FOUNDATION_MEMORY_ID is REQUIRED — all other fields have safe
    defaults. Raises ValueError with a structured message if memory_id is missing.
    """
    from agents.supervisor.persona_loader import PersonaLoaderConfig  # avoid circular at module level

    memory_id = os.environ.get("VSA_FOUNDATION_MEMORY_ID", "")
    if not memory_id:
        raise ValueError(
            "VSA_FOUNDATION_MEMORY_ID is required but not set. "
            "Set it to the AgentCore Memory ID for this deployment."
        )

    adp_region = os.environ.get("VSA_ADP_REGION", "us-east-1")
    adp_stage = os.environ.get("VSA_ADP_STAGE", "staging")

    adp_account_id = os.environ.get("VSA_ADP_ACCOUNT_ID", "")
    if not adp_account_id:
        adp_account_id = boto3.client("sts").get_caller_identity()["Account"]

    athena_workgroup = os.environ.get(
        "VSA_ATHENA_WORKGROUP", f"cvx-{adp_stage}-analytics"
    )
    athena_results_bucket = os.environ.get(
        "VSA_ATHENA_RESULTS_BUCKET",
        f"cvx-{adp_stage}-athena-results-{adp_account_id}-us-east-1",
    )

    ttl_minutes = int(os.environ.get("VSA_PERSONA_LTM_TTL_MIN", "15"))
    adp_kb_id = os.environ.get("VSA_ADP_KB_ID", "")
    memory_region = os.environ.get("VSA_MEMORY_REGION", "us-west-2")

    adp_kb_arn = (
        f"arn:aws:bedrock:{adp_region}:{adp_account_id}:knowledge-base/{adp_kb_id}"
        if adp_kb_id
        else ""
    )

    return PersonaLoaderConfig(
        memory_id=memory_id,
        adp_account_id=adp_account_id,
        adp_region=adp_region,
        adp_stage=adp_stage,
        athena_workgroup=athena_workgroup,
        athena_results_bucket=athena_results_bucket,
        ttl_minutes=ttl_minutes,
        adp_kb_id=adp_kb_id,
        adp_kb_arn=adp_kb_arn,
        memory_region=memory_region,
    )
