"""Tool factories for the VSA supervisor.

Each tool is a factory that takes a SessionContext and a Config and
returns a @tool-decorated function ready to register with Strands Agent.
This keeps tenantId, JWT, and sessionId out of the LLM-visible signature
while still making them available at call time.

Tools that query ADP data products (vehicle_identity, service_network,
parts_lookup, knowledge) don't need session context — they're stateless
lookups configured via ADP_STAGE env var.
"""

from agents.supervisor.tools.book import make_book_tool
from agents.supervisor.tools.fleet_health import make_fleet_health_tool
from agents.supervisor.tools.daily_briefing import make_daily_briefing_tool
from agents.supervisor.tools.fleet_actions import make_fleet_actions_tool
from agents.supervisor.tools.decision_journal import make_decision_journal_tool
from agents.supervisor.tools.health_findings import make_health_findings_tool
from agents.supervisor.tools.kb_filter import make_kb_filter_tool
from agents.supervisor.tools.catalog_compare import make_catalog_compare_tool
from agents.supervisor.tools.catalog_query import make_catalog_query_tool
from agents.supervisor.tools.charging_history import make_charging_history_tool
from agents.supervisor.tools.configurator_recommend import make_configurator_recommend_tool
from agents.supervisor.tools.customer_profile import make_customer_profile_tool
from agents.supervisor.tools.delivery_status import make_delivery_status_tool
from agents.supervisor.tools.diy_repair_advisor import make_diy_repair_advisor_tool
from agents.supervisor.tools.energy_summary import make_energy_summary_tool
from agents.supervisor.tools.escalate import (
    make_escalate_tool,
    make_service_escalate_tool,
)
from agents.supervisor.tools.interaction_history import make_interaction_history_tool
from agents.supervisor.tools.knowledge import make_knowledge_tool
from agents.supervisor.tools.lead_capture import make_lead_capture_tool
from agents.supervisor.tools.manufacturing_explain import make_manufacturing_explain_tool
from agents.supervisor.tools.offers_lookup import make_offers_lookup_tool
from agents.supervisor.tools.ota_history import make_ota_history_tool
from agents.supervisor.tools.parts_lookup import make_parts_lookup_tool
from agents.supervisor.tools.resolve_or_create_owner_identity import make_resolve_or_create_owner_identity_tool
from agents.supervisor.tools.retrieve import make_retrieve_tool
from agents.supervisor.tools.service_history import make_service_history_tool
from agents.supervisor.tools.service_network import make_service_network_tool
from agents.supervisor.tools.telemetry_summary import make_telemetry_summary_tool
from agents.supervisor.tools.triage import make_triage_tool
from agents.supervisor.tools.variant_compare import make_variant_compare_tool
from agents.supervisor.tools.vehicle_identity import make_vehicle_identity_tool
# CVX DMS dealer-facing persona tools (spec 2026-08-26-cvx-dms-personas-extension)
from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool, make_warranty_suggest_tool
from agents.supervisor.tools.dms_copilot import make_visit_brief_tool, make_portfolio_summary_tool, make_action_plan_tool
from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool
from agents.supervisor.tools.dms_stubs import make_parts_intel_stub_tool, make_compliance_agent_stub_tool
from agents.supervisor.tools.parts_intel import make_parts_intel_tool

__all__ = [
    "make_triage_tool",
    "make_retrieve_tool",
    "make_book_tool",
    "make_health_findings_tool",
    "make_kb_filter_tool",
    "make_diy_repair_advisor_tool",
    "make_escalate_tool",
    "make_service_escalate_tool",
    "make_knowledge_tool",
    "make_vehicle_identity_tool",
    "make_service_network_tool",
    "make_parts_lookup_tool",
    "make_service_history_tool",
    "make_charging_history_tool",
    "make_customer_profile_tool",
    "make_interaction_history_tool",
    "make_ota_history_tool",
    "make_telemetry_summary_tool",
    "make_energy_summary_tool",
    # CVX Acquire tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-21)
    "make_configurator_recommend_tool",
    "make_variant_compare_tool",           # renamed from make_configurator_compare_tool
    "make_delivery_status_tool",
    "make_manufacturing_explain_tool",
    "make_resolve_or_create_owner_identity_tool",
    # CVX Discover tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-28)
    "make_catalog_query_tool",
    "make_offers_lookup_tool",
    "make_catalog_compare_tool",
    "make_lead_capture_tool",
    # CVX Fleet Manager tools (spec 2026-08-21-cvx-fleet-manager-persona)
    "make_fleet_health_tool",
    "make_daily_briefing_tool",
    "make_fleet_actions_tool",
    "make_decision_journal_tool",
    # CVX DMS dealer-facing persona tools (spec 2026-08-26-cvx-dms-personas-extension)
    "make_warranty_validate_tool",
    "make_warranty_suggest_tool",
    "make_visit_brief_tool",
    "make_portfolio_summary_tool",
    "make_action_plan_tool",
    "make_classify_bulletin_tool",
    "make_parts_intel_tool",
    "make_compliance_agent_stub_tool",
    "PERSONA_TOOL_FACTORIES",
]

# Per spec § "Persona-to-data-product matrix" — tool factory lists per persona.
# Order is intentional (deterministic agent-tool registration). Do NOT alphabetise.
PERSONA_TOOL_FACTORIES: dict[str, list] = {
    "fleet_driver": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
        make_service_escalate_tool,
        make_vehicle_identity_tool,
        make_service_history_tool,
        make_charging_history_tool,
        make_ota_history_tool,
        make_telemetry_summary_tool,
        make_energy_summary_tool,
        make_service_network_tool,
        make_diy_repair_advisor_tool,
        # Tier 2 platform health findings (spec 2026-08-03-cvx-tier2-platform-health-agent)
        make_health_findings_tool,
        # Parts intelligence orchestrator (spec 2026-09-10-dms-parts-intel)
        make_parts_intel_tool,
    ],
    "consumer": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
        make_service_escalate_tool,
        make_vehicle_identity_tool,
        make_charging_history_tool,
        make_customer_profile_tool,
        make_interaction_history_tool,
        make_ota_history_tool,
        make_energy_summary_tool,
        make_service_network_tool,
        # Acquire tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-21)
        make_configurator_recommend_tool,
        make_variant_compare_tool,
        make_delivery_status_tool,
        make_manufacturing_explain_tool,
        make_resolve_or_create_owner_identity_tool,
        # Discover tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-28)
        make_catalog_query_tool,
        make_offers_lookup_tool,
        make_catalog_compare_tool,
        make_lead_capture_tool,
        # Tier 2 platform health findings (spec 2026-08-03-cvx-tier2-platform-health-agent)
        make_health_findings_tool,
        # Parts intelligence orchestrator (spec 2026-09-10-dms-parts-intel)
        make_parts_intel_tool,
    ],
    "retail_advisor": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
        make_service_escalate_tool,
        make_vehicle_identity_tool,
        make_customer_profile_tool,
        make_interaction_history_tool,
        make_service_network_tool,
        # Acquire tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-21)
        make_configurator_recommend_tool,
        make_variant_compare_tool,
        make_delivery_status_tool,
        make_manufacturing_explain_tool,
        make_resolve_or_create_owner_identity_tool,
        # Discover tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-28)
        make_catalog_query_tool,
        make_offers_lookup_tool,
        make_catalog_compare_tool,
        make_lead_capture_tool,
    ],
    "vehicle_owner": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
        make_service_escalate_tool,
        make_vehicle_identity_tool,
        make_service_history_tool,
        make_charging_history_tool,
        make_customer_profile_tool,
        make_ota_history_tool,
        make_telemetry_summary_tool,
        make_service_network_tool,
        # Acquire tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-21)
        make_configurator_recommend_tool,
        make_variant_compare_tool,
        make_delivery_status_tool,
        make_manufacturing_explain_tool,
        make_resolve_or_create_owner_identity_tool,
        # Discover tools (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-28)
        make_catalog_query_tool,
        make_offers_lookup_tool,
        make_catalog_compare_tool,
        make_lead_capture_tool,
        # Tier 2 platform health findings (spec 2026-08-03-cvx-tier2-platform-health-agent)
        make_health_findings_tool,
    ],
    "service_advisor": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
        make_service_escalate_tool,
        make_knowledge_tool,
        make_vehicle_identity_tool,
        make_service_history_tool,
        make_customer_profile_tool,
        make_interaction_history_tool,
        make_parts_lookup_tool,
        make_service_network_tool,
        make_diy_repair_advisor_tool,
        # Tier 2 platform health findings (spec 2026-08-03-cvx-tier2-platform-health-agent)
        make_health_findings_tool,
        # Parts intelligence orchestrator (spec 2026-09-10-dms-parts-intel)
        make_parts_intel_tool,
    ],
    "technician": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
        make_service_escalate_tool,
        make_knowledge_tool,
        make_vehicle_identity_tool,
        make_service_history_tool,
        make_parts_lookup_tool,
        make_service_network_tool,
        make_diy_repair_advisor_tool,
        # Parts intelligence orchestrator (spec 2026-09-10-dms-parts-intel)
        make_parts_intel_tool,
    ],
    # CVX Fleet Manager persona (spec 2026-08-21-cvx-fleet-manager-persona).
    # Fleet-aggregate, no VIN. Group-gated in handler.py: honored only for callers
    # whose cognito:groups intersect {platform-admin, fleet-operator, fleet-viewer}.
    # See spec § Decision and handler.py _GROUP_GATED_REQUESTABLE_PERSONAS.
    "fleet_manager": [
        make_triage_tool,            # symptom triage if a vehicle concern surfaces
        make_retrieve_tool,          # KB grounding for policy / maintenance docs
        make_fleet_health_tool,      # GET /api/v1/fleet-health
        make_daily_briefing_tool,    # GET /api/v1/daily-briefing
        make_fleet_actions_tool,     # GET /api/v1/fleet-actions (read-only)
        make_decision_journal_tool,  # GET /api/v1/decision-journal
        make_escalate_tool,
        make_service_escalate_tool,          # human hand-off
        make_service_network_tool,   # locate service centres for fleet dispatch
    ],
    # ---------------------------------------------------------------------------
    # CVX DMS dealer-facing personas (spec 2026-08-26-cvx-dms-personas-extension).
    # All Tier 1. All tools are thin DynamoDB reads — no LLM calls inside tools.
    # ---------------------------------------------------------------------------
    # warranty_agent: active — DMS warranty rules-engine verdict reader.
    # Claims access is scoped by the caller's Cognito custom:dealerIds claims
    # (enforced by the DMS rules engine, not duplicated here).
    "warranty_agent": [
        make_warranty_validate_tool,  # DDB dms-{stage}-warranty-claims: verdict VERBATIM
        make_warranty_suggest_tool,   # DDB dms-{stage}-warranty-claims: correction hints
        make_retrieve_tool,           # KB grounding for warranty policy docs
        make_escalate_tool,
        make_service_escalate_tool,           # human hand-off to brand warranty specialist
    ],
    # dm_copilot: active — district manager co-pilot, reads Tier 2 artifacts.
    # portfolio_summary scoped by caller's Cognito custom:districtIds claims.
    #
    # Per decisions.md 2026-09-03 (c): make_triage_tool removed.  A district
    # manager triaging a vehicle concern crosses into service_advisor scope,
    # which is what persona separation exists to prevent.  A DM who needs
    # triage escalates to a service_advisor session instead.
    "dm_copilot": [
        make_visit_brief_tool,        # DDB dms-{stage}-dm-visit-briefs (Tier 2 artifact)
        make_portfolio_summary_tool,  # DDB dms-{stage}-dm-portfolio
        make_action_plan_tool,        # DDB dms-{stage}-dm-action-plans
        make_retrieve_tool,           # KB grounding for brand standards / policy docs
        make_escalate_tool,
        make_service_escalate_tool,           # human hand-off to brand/regional support
    ],
    # comms_triage: active — communications bulletin classification reader.
    "comms_triage": [
        make_classify_bulletin_tool,  # DDB dms-{stage}-comms-bulletins: pre-computed classification
        make_retrieve_tool,           # KB grounding for brand standards / regulatory refs
        make_escalate_tool,
        make_service_escalate_tool,           # human hand-off for high-urgency bulletins
    ],
    # parts_intel: active (spec 2026-09-10-dms-parts-intel) — KB + dispatch orchestrator.
    # Tier 1, chat only (not bidi). Dispatch step gated on fleet-operator group.
    "parts_intel": [
        make_parts_intel_tool,
    ],
    # compliance_agent: stub — see parts_intel note.
    "compliance_agent": [
        make_compliance_agent_stub_tool,
    ],
    # ---------------------------------------------------------------------------
    # fleet_owner: small-business fleet owner (iOS-first) — spec
    # 2026-09-18-avx-tier2-ios-cards-and-actions § "Personas (Spec 1a scope)".
    # iOS card feed + chat drill-down surface. Use cases: #8 weekly attention
    # (per-VIN roll-up) and #11 compliance monitor (recall/inspection-due).
    # NOT fleet_manager (CMS-web; Spec 3). Group-gated in handler.py /
    # persona_map.py: honored only for callers whose cognito:groups contains
    # "fleet-owner".
    # ---------------------------------------------------------------------------
    "fleet_owner": [
        make_triage_tool,            # symptom triage when a VIN issue surfaces
        make_retrieve_tool,          # KB grounding for policy / recall / compliance docs
        make_vehicle_identity_tool,  # make, model, year (no recall data)
        make_service_history_tool,   # service visits and open work orders per VIN
        make_telemetry_summary_tool, # recent telemetry aggregates for weekly attention
        make_energy_summary_tool,    # energy-usage trends (EV/PHEV fleet context)
        make_service_network_tool,   # locate service centres for fleet dispatch
        make_book_tool,              # schedule service for a fleet vehicle
        make_escalate_tool,
        make_service_escalate_tool,          # human hand-off to fleet support
        # Tier 2 platform health findings (spec 2026-08-03; AVX iOS card feed
        # reads from this on the fleet_owner surface per Spec 1a scope).
        make_health_findings_tool,
    ],
}
