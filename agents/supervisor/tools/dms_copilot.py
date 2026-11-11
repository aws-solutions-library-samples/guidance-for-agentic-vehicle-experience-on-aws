"""DMS dm_copilot tools — district manager co-pilot, thin DynamoDB readers.

All three tools READ pre-computed Tier 2 artifacts from DynamoDB tables:

  visit_brief()        → dms-{stage}-dm-visit-briefs    (PK: dealer_id, SK: computed_at)
                         + dms-{stage}-avx-pre-briefs   (PK: dealer_id,
                           SK: appointment_date_ro_id = {date}#{ro_id}#{computed_at})
                           read as an optional SUPERSET — DMS Spec 2 Task 3.4.
                           A base brief is REQUIRED for available:True; the AVX
                           artifact augments, it cannot substitute.
                           NOTE the SKs differ in meaning: descending on the base
                           table gives the newest COMPUTED brief, but on the AVX
                           table it gives the latest APPOINTMENT DATE, since
                           appointment_date is the SK's first component.
  portfolio_summary()  → dms-{stage}-dealer-performance (district rollup)
  action_plan()        → dms-{stage}-dm-action-plans    (PK: dealer_id)

Every one of these tables has a COMPOSITE primary key, so a point ``GetItem``
supplying only ``dealer_id`` raises ``ValidationException``.  ``visit_brief``
reads via ``_query_latest_by_dealer`` (Query, newest-first, Limit=1) for this
reason.  ``portfolio_summary`` and ``action_plan`` still issue the incorrect
point lookup and are non-functional — tracked in CVX
``issues/2026-09-19-dms-copilot-getitem-key-schema-mismatch/``; they are the
owning spec's to fix, and ``portfolio_summary`` needs a design answer first.

Table names verified against
``~/guidance-for-dealer-management-system-on-aws/deployment/stacks/dms_storage_stack.py:71``
— the authoritative list of DMS DDB tables.  ``dealer-performance`` is the
district-level rollup surface (NOT ``dm-portfolio``, which does not exist
in DMS storage).

KEY CONTRACT: ``visit_brief()`` MUST surface ``computed_at`` so the agent can
say "as of Tuesday" rather than presenting a stale number as live.  It must
NOT recompute — that is the Tier 2 agent's job.  When no artifact exists it
returns ``{"available": False, "reason": "no brief computed yet"}``.

NO LLM call inside any tool — see tools/sub_agent.py docstring for why.
Region: us-west-2 (see spec Constraints).

**Two gates, in order.**

  1. Certification-tier gate (spec § Constraint 4) — caller-scoped.  Consults
     the caller's dealer tier (never the target dealer's) so AI activation
     is a per-caller entitlement.  Below Gold → deterministic non-AI response.
  2. Caller-to-target scope check (spec § Constraints — "the agent does not
     widen scope") — pre-read.  ``visit_brief`` and ``action_plan`` verify
     the requested ``dealer_id`` is in the caller's ``dealer_ids`` claims;
     ``portfolio_summary`` verifies ``district_id`` is in ``district_ids``.
     Out of scope → ``no_access_response()``.  DDB is not read.

The prior implementation gated ``visit_brief`` / ``action_plan`` on the
*target* dealer's tier — a Bronze-tier caller could invoke
``visit_brief("Gold-dealer")`` and receive the Tier-2 artifact.  Corrected
2026-09-04 per ``security-review.md`` Cycle 1 Warning 1.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Key
from strands import tool

from agents.supervisor._lib.dms_cert_cache import (
    check_dealer_certification,
    dealer_in_scope,
    district_in_scope,
    deterministic_response_for_tier,
    is_ai_activated,
    no_access_response,
    resolve_dealer_id,
)
from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

_DMS_STAGE = os.environ.get("VSA_DMS_STAGE", "staging")
_DMS_REGION = os.environ.get("VSA_DMS_REGION", "us-west-2")
_VISIT_BRIEFS_TABLE_TPL = "dms-{stage}-dm-visit-briefs"
# AVX pre-brief artifact (DMS Spec 2) — the richer, per-appointment Tier 2
# artifact written by DMS's pre-brief agent.  Read as a SUPERSET on top of
# the base visit brief: present -> the response gains ``avx_evidence``;
# absent or unreadable -> the response is byte-identical to the base shape.
# Env var name matches the DMS writer's own override
# (``source/tier2/pre_brief_agent.py``) so a single name is used on both sides.
_AVX_PRE_BRIEFS_TABLE_TPL = "dms-{stage}-avx-pre-briefs"
# District-level performance rollup — DMS's authoritative name for this
# surface is ``dealer-performance`` (not ``dm-portfolio``, which the
# earlier code invented).  Verified 2026-09-04 against DMS
# dms_storage_stack.py:170.
_PORTFOLIO_TABLE_TPL = "dms-{stage}-dealer-performance"
_ACTION_PLANS_TABLE_TPL = "dms-{stage}-dm-action-plans"

_UNAVAILABLE_RESPONSE: dict[str, Any] = {
    "available": False,
    "reason": "DMS table temporarily unreachable",
}


def _visit_briefs_table() -> str:
    return _VISIT_BRIEFS_TABLE_TPL.format(stage=_DMS_STAGE)


def _avx_pre_briefs_table() -> str:
    return os.environ.get(
        "DMS_AVX_PRE_BRIEFS_TABLE",
        _AVX_PRE_BRIEFS_TABLE_TPL.format(stage=_DMS_STAGE),
    )


def _query_latest_visit_brief(dealer_id: str) -> dict[str, Any] | None:
    """Return the newest base visit brief for ``dealer_id``, or None.

    ``dms-{stage}-dm-visit-briefs`` is keyed (PK=``dealer_id``, SK=``computed_at``),
    so a descending Query with ``Limit=1`` genuinely yields the most recently
    computed brief.

    A point ``GetItem`` is NOT usable: the table has a composite primary key and
    supplying only the partition key raises ``ValidationException``. That was the
    pre-existing defect on this read path — see CVX
    ``issues/2026-09-19-dms-copilot-getitem-key-schema-mismatch/``.

    Raises:
        BotoCoreError, ClientError: propagated; the caller surfaces a base-table
            failure as unavailable.
    """
    return _query_newest_by_sk(_visit_briefs_table(), dealer_id)


def _query_pre_brief_for_latest_appointment(dealer_id: str) -> dict[str, Any] | None:
    """Return the AVX pre-brief for the dealer's LATEST APPOINTMENT DATE, or None.

    Named for what it actually selects. ``dms-{stage}-avx-pre-briefs`` is keyed
    (PK=``dealer_id``, SK=``appointment_date_ro_id``) where the SK value is the
    composite ``{appointment_date}#{ro_id}#{computed_at}``. A descending Query
    therefore orders by **appointment_date first**, so ``Limit=1`` selects the
    furthest-future appointment — NOT the most recently computed artifact.

    Those two differ whenever a dealer has more than one upcoming appointment,
    and no index on this table answers "newest computed pre-brief per dealer":
    ``computed_at`` is only the third component of the sort key. Rather than
    paginate the whole partition to sort client-side, the caller surfaces
    ``avx_appointment_date`` / ``avx_ro_id`` / ``avx_computed_at`` alongside the
    evidence, so a narrator can say which visit the evidence describes and how
    stale it is. Evidence without that context is not narratable.

    Raises:
        BotoCoreError, ClientError: propagated; the caller degrades an AVX-table
            failure to "absent" so the base brief still serves.
    """
    return _query_newest_by_sk(_avx_pre_briefs_table(), dealer_id)


def _query_newest_by_sk(table_name: str, dealer_id: str) -> dict[str, Any] | None:
    """Query one dealer-keyed table descending by sort key, returning the top item.

    Shared mechanics only. What "top" MEANS depends on the table's sort key —
    see the two callers, which differ on exactly that point.
    """
    ddb = boto3.resource("dynamodb", region_name=_DMS_REGION)
    tbl = ddb.Table(table_name)
    resp = tbl.query(
        KeyConditionExpression=Key("dealer_id").eq(dealer_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _emit_avx_read_failure_metric(exc_type: str) -> None:
    """Emit ``avx_read_failure`` in the ``dms/visit_brief`` namespace via EMF.

    EMF rather than ``cloudwatch:PutMetricData``: a structured log line is
    extracted by CloudWatch Logs with no additional IAM grant, so observing this
    failure does not widen the AgentCore role. (The sibling ``retrieve.py`` uses
    PutMetricData; that predates this decision and would require a grant here.)

    Written straight to stdout, NOT through ``log``. CloudWatch extracts EMF only
    when the ENTIRE log event is the JSON blob, and every entrypoint — including
    the deployed ``agents/supervisor/agentcore_app.py`` — calls
    ``logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s")``.
    Emitting through the logger would produce ``2026-09-19 ... INFO {...}``, which
    fails ``json.loads`` at the CloudWatch end, silently yielding a metric that
    reads zero forever and therefore looks like health. Review Cycle 2 Warning 1.

    A dedicated logger with a message-only formatter would also work, but
    ``StreamHandler`` binds its stream at construction and the arrangement can be
    undone by later logging config; a direct write resolves ``sys.stdout`` at call
    time and has no formatter to defeat. In the AgentCore container stdout is
    ingested as one CloudWatch event per line, which is what EMF needs.

    Why a metric at all: the AVX read degrades to "absent" on failure, which is
    correct for availability but indistinguishable — in the response — from a
    dealer who simply has no pre-brief. A permanent failure (grant absent, key
    schema drift) would make ``avx_evidence`` vanish from every response
    indefinitely while every response still looked valid. Best-effort; never
    raises.

    NOTE: nothing currently consumes this namespace. The metric is the signal;
    an alarm on it is a follow-on (see the DMS spec's decisions.md).
    """
    try:
        sys.stdout.write(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "dms/visit_brief",
                    "Dimensions": [["exception_type"]],
                    "Metrics": [{"Name": "avx_read_failure", "Unit": "Count"}],
                }],
            },
            "exception_type": exc_type,
            "avx_read_failure": 1,
        }) + "\n")
        sys.stdout.flush()  # containers buffer; an unflushed metric is no metric
    except Exception:  # noqa: BLE001
        pass  # observability must never break the tool


def _portfolio_table() -> str:
    return _PORTFOLIO_TABLE_TPL.format(stage=_DMS_STAGE)


def _action_plans_table() -> str:
    return _ACTION_PLANS_TABLE_TPL.format(stage=_DMS_STAGE)


def make_visit_brief_tool(ctx: SessionContext, config: Config):
    """Build the ``visit_brief`` tool bound to a session context.

    Reads the Tier 2 artifact written by DMS's nightly visit-brief agent
    (DMS spec T7.4).  Does NOT recompute.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that returns a dealer visit brief.
    """
    # Retained for the "configured" guard below. The readers resolve their own
    # table names, so the AVX table is not bound here.
    _table = _visit_briefs_table()

    @tool
    def visit_brief(dealer_id: str) -> dict[str, Any]:
        """Retrieve the pre-computed Tier 2 visit brief for a specific dealer.

        ALWAYS surface the ``computed_at`` timestamp when narrating this
        brief.  Never present the content as if it were computed live.  If
        no brief has been computed yet, say so plainly — do NOT fabricate.

        Args:
            dealer_id: The DMS dealer identifier (e.g. "DLR-0042").

        Returns:
            The Tier 2 artifact dict when the caller is AI-activated (Gold+)
            AND ``dealer_id`` is in ``ctx.dealer_ids``.  Below-Gold callers
            get the deterministic response; out-of-scope callers get
            ``no_access_response()``; missing artifacts return
            ``{"available": False, "reason": "no brief computed yet"}``.

            When a DMS AVX pre-brief artifact exists for the dealer, the
            response additionally carries ``avx_evidence`` plus the context
            identifying it — ``avx_appointment_date``, ``avx_ro_id`` and
            ``avx_computed_at``. When no such artifact exists the response is
            byte-identical to the base shape, so pre-existing ``dm_copilot``
            consumers are unaffected.

            The AVX read selects the pre-brief for the dealer's LATEST
            APPOINTMENT DATE, which is not necessarily the most recently
            computed one; narrate ``avx_appointment_date`` so the user knows
            which visit the extra evidence describes.

            A base brief is REQUIRED for ``available: True``. An AVX pre-brief
            alone yields "no brief computed yet" — see the comment at the read
            site for why the two artifacts must not be conflated.
        """
        # Gate 1: cert-tier — caller-scoped (NOT target-scoped; see module
        # docstring).
        tier, _ = check_dealer_certification(resolve_dealer_id(ctx))
        if not is_ai_activated(tier):
            return deterministic_response_for_tier(tier)

        # Gate 2: scope — pre-read.  A caller can only visit-brief a dealer
        # inside their authorized dealer_ids claim set.
        if not dealer_in_scope(ctx, dealer_id):
            return no_access_response(dealer_id)

        if not _table:
            return {"available": False, "reason": "DMS table not configured"}

        # The BASE brief is required. An AVX pre-brief alone cannot stand in for
        # it: the four narrative keys below (priority_items, service_forecast,
        # open_concerns, recommendations) exist only on the base artifact, and
        # rendering them as []/None for a dealer whose brief was never computed
        # states "no open concerns" as though that had been determined. It would
        # also route around the persona's only anti-fabrication instruction,
        # which keys on the literal phrase "no brief computed yet"
        # (persona_definitions.py). A sparse base item and an absent base item
        # are different facts and must not collapse. Review Cycle 1 Critical.
        try:
            item = _query_latest_visit_brief(dealer_id)
        except (BotoCoreError, ClientError) as exc:
            log.warning("visit_brief: DDB error for %s: %s", dealer_id, exc)
            return _UNAVAILABLE_RESPONSE

        if not item:
            # Per spec R4: return the exact phrase, never fabricate.
            return {"available": False, "reason": "no brief computed yet"}

        result: dict[str, Any] = {
            "available": True,
            "dealer_id": dealer_id,
            # computed_at MUST be present so the agent can narrate staleness.
            "computed_at": item.get("computed_at"),
            "agent_version": item.get("agent_version"),
            "confidence": item.get("confidence"),
            "inputs_hash": item.get("inputs_hash"),
            "evidence": item.get("evidence", []),
            "priority_items": item.get("priority_items", []),
            "service_forecast": item.get("service_forecast"),
            "open_concerns": item.get("open_concerns", []),
            "recommendations": item.get("recommendations", []),
        }

        # AVX pre-brief (DMS Spec 2) — an OPTIONAL superset, read only once the
        # base brief is known to exist. A failure degrades to "absent" rather
        # than to _UNAVAILABLE_RESPONSE, because an unreachable enhancement
        # table must not break the pre-existing consumer. This is a deliberate,
        # tested, METERED fall-through rather than a silent swallow: it logs the
        # exception TYPE (never str(exc)) and emits an EMF metric, so a
        # permanent failure is distinguishable from a dealer who simply has no
        # pre-brief. Review Cycle 1 Warning.
        try:
            avx_item = _query_pre_brief_for_latest_appointment(dealer_id)
        except (BotoCoreError, ClientError) as exc:
            log.warning(
                "visit_brief: AVX pre-brief read failed for %s (%s); "
                "serving the base brief without avx_evidence",
                dealer_id,
                type(exc).__name__,
            )
            _emit_avx_read_failure_metric(type(exc).__name__)
            avx_item = None

        if avx_item:
            result["avx_evidence"] = avx_item.get("evidence", [])
            # Identify WHICH visit the evidence describes and how stale it is.
            # The AVX read selects the latest APPOINTMENT DATE, not the newest
            # artifact, so evidence stripped of this context would be
            # unattributable — see _query_pre_brief_for_latest_appointment.
            result["avx_appointment_date"] = avx_item.get("appointment_date")
            result["avx_ro_id"] = avx_item.get("ro_id")
            result["avx_computed_at"] = avx_item.get("computed_at")
        return result

    return visit_brief


def make_portfolio_summary_tool(ctx: SessionContext, config: Config):
    """Build the ``portfolio_summary`` tool bound to a session context.

    Reads the district-level performance rollup from
    ``dms-{stage}-dealer-performance``.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that returns a district portfolio summary.
    """
    _table = _portfolio_table()

    @tool
    def portfolio_summary(district_id: str) -> dict[str, Any]:
        """Retrieve the portfolio performance summary for a district —
        aggregate metrics across all dealers in the district.

        Args:
            district_id: The DMS district identifier (e.g. "DST-WEST-01").

        Returns:
            The district rollup when AI-activated AND ``district_id`` is in
            ``ctx.district_ids``.  Otherwise deterministic or no-access.
        """
        # Gate 1: cert-tier — caller-scoped.  A DM with no dealer id on their
        # session is UNKNOWN (fail-closed to deterministic).
        tier, _ = check_dealer_certification(resolve_dealer_id(ctx))
        if not is_ai_activated(tier):
            return deterministic_response_for_tier(tier)

        # Gate 2: scope — a caller can only summarize a district in their
        # authorized district_ids claim set.
        if not district_in_scope(ctx, district_id):
            return no_access_response(district_id)

        if not _table:
            return {"available": False, "reason": "DMS table not configured"}

        try:
            ddb = boto3.resource("dynamodb", region_name=_DMS_REGION)
            tbl = ddb.Table(_table)
            resp = tbl.get_item(Key={"district_id": district_id})
        except (BotoCoreError, ClientError) as exc:
            log.warning("portfolio_summary: DDB error for %s: %s", district_id, exc)
            return _UNAVAILABLE_RESPONSE

        item = resp.get("Item")
        if not item:
            return {"available": False, "reason": "District not found"}

        return {
            "available": True,
            "district_id": district_id,
            "computed_at": item.get("computed_at"),
            "dealer_count": item.get("dealer_count"),
            "top_performers": item.get("top_performers", []),
            "needs_attention": item.get("needs_attention", []),
            "aggregate": item.get("aggregate", {}),
        }

    return portfolio_summary


def make_action_plan_tool(ctx: SessionContext, config: Config):
    """Build the ``action_plan`` tool bound to a session context.

    Reads per-dealer action items from the DMS action-plans table.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that returns a dealer's action plan.
    """
    _table = _action_plans_table()

    @tool
    def action_plan(dealer_id: str) -> dict[str, Any]:
        """Retrieve the current action plan for a specific dealer — the
        ordered list of improvement actions with due dates, status, and owner.

        Args:
            dealer_id: The DMS dealer identifier (e.g. "DLR-0042").

        Returns:
            The action plan when AI-activated AND authorized.  Otherwise
            deterministic or no-access.
        """
        # Gate 1: cert-tier — caller-scoped.
        tier, _ = check_dealer_certification(resolve_dealer_id(ctx))
        if not is_ai_activated(tier):
            return deterministic_response_for_tier(tier)

        # Gate 2: scope — pre-read.
        if not dealer_in_scope(ctx, dealer_id):
            return no_access_response(dealer_id)

        if not _table:
            return {"available": False, "reason": "DMS table not configured"}

        try:
            ddb = boto3.resource("dynamodb", region_name=_DMS_REGION)
            tbl = ddb.Table(_table)
            resp = tbl.get_item(Key={"dealer_id": dealer_id})
        except (BotoCoreError, ClientError) as exc:
            log.warning("action_plan: DDB error for %s: %s", dealer_id, exc)
            return _UNAVAILABLE_RESPONSE

        item = resp.get("Item")
        if not item:
            return {"available": False, "reason": "No action plan found"}

        return {
            "available": True,
            "dealer_id": dealer_id,
            "updated_at": item.get("updated_at"),
            "actions": item.get("actions", []),
        }

    return action_plan
