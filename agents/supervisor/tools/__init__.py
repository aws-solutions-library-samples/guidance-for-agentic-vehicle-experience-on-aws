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
from agents.supervisor.tools.escalate import make_escalate_tool
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

__all__ = [
    "make_triage_tool",
    "make_retrieve_tool",
    "make_book_tool",
    "make_health_findings_tool",
    "make_kb_filter_tool",
    "make_diy_repair_advisor_tool",
    "make_escalate_tool",
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
    ],
    "consumer": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
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
    ],
    "retail_advisor": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
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
    ],
    "technician": [
        make_triage_tool,
        make_retrieve_tool,
        make_book_tool,
        make_escalate_tool,
        make_knowledge_tool,
        make_vehicle_identity_tool,
        make_service_history_tool,
        make_parts_lookup_tool,
        make_service_network_tool,
        make_diy_repair_advisor_tool,
    ],
}
