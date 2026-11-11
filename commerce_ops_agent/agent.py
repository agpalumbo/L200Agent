"""Multi-Agent Google ADK Orchestration Graph & Resumable App Definition.

Implements proven multi-agent patterns rather than a monolith:
1. `edge_catalog_inspector` (`gemini-2.5-flash`): Fast parallel Cloudflare + Shopify catalog checks.
2. `order_margin_reconciler` (`gemini-2.5-flash`): Fast parallel PII-redacted order & SLA checks.
3. `parallel_store_diagnostics` (`ParallelAgent`): Concurrent fan-out across edge and commerce SoRs.
4. `remediation_planner` (`gemini-2.5-pro`): Root-cause reasoning, historical memory lookup, and
   Human-in-the-Loop gated remediations.
5. `daily_reconciliation_pipeline` (`SequentialAgent`): Deterministic end-to-end ops workflow.
6. `root_agent` (`commerce_ops_coordinator`, `gemini-2.5-pro`): Strategic coordinator routing
   targeted lookups vs. full parallel/sequential store reconciliations.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.apps.app import ContextCacheConfig, EventsCompactionConfig
from google.adk.plugins.context_filter_plugin import ContextFilterPlugin

from .config import get_settings
from .constitution import (
    AGENT_CONSTITUTION,
    EDGE_INSPECTOR_INSTRUCTION,
    ORDER_RECONCILER_INSTRUCTION,
    REMEDIATION_PLANNER_INSTRUCTION,
)
from .observability.telemetry import init_telemetry
from .security.guardrails_plugin import CommerceGovernancePlugin
from .tools import (
    create_shopify_recovery_discount,
    fetch_redacted_shopify_orders,
    inspect_cloudflare_edge_routes,
    purge_cloudflare_product_cache,
    reconcile_order_margins_and_fulfillment_sla,
    search_operational_incident_memory,
    verify_shopify_storefront_catalog_parity,
)

init_telemetry()
_SETTINGS = get_settings()

# 1. Fast Specialist Sub-Agents (`gemini-2.5-flash`)
edge_catalog_inspector = LlmAgent(
    name="edge_catalog_inspector",
    model=_SETTINGS.specialist_model,
    description=(
        "Fast specialist that inspects Cloudflare storefront edge routes on alottacake.com "
        "and verifies product price and visibility parity against Shopify."
    ),
    instruction=EDGE_INSPECTOR_INSTRUCTION,
    tools=[
        inspect_cloudflare_edge_routes,
        verify_shopify_storefront_catalog_parity,
    ],
    output_key="edge_diagnostic_report",
)

order_margin_reconciler = LlmAgent(
    name="order_margin_reconciler",
    model=_SETTINGS.specialist_model,
    description=(
        "Fast specialist that fetches PII-redacted, HMAC-pseudonymized Shopify orders and "
        "computes exact minor-unit contribution margins and fulfillment draft SLA breaches."
    ),
    instruction=ORDER_RECONCILER_INSTRUCTION,
    tools=[
        fetch_redacted_shopify_orders,
        reconcile_order_margins_and_fulfillment_sla,
    ],
    output_key="order_reconciliation_report",
)

# 2. Concurrent Fan-Out (`ParallelAgent`) + Synthesis & HITL Gate (`SequentialAgent`)
parallel_store_diagnostics = ParallelAgent(
    name="parallel_store_diagnostics",
    description="Runs Cloudflare edge parity and Shopify order/margin checks concurrently.",
    sub_agents=[edge_catalog_inspector, order_margin_reconciler],
)

remediation_planner = LlmAgent(
    name="remediation_planner",
    model=_SETTINGS.coordinator_model,
    description=(
        "Strategic planner that synthesizes edge and order diagnostics, consults persistent "
        "incident memory, and executes HITL-gated cache purges or recovery discounts."
    ),
    instruction=REMEDIATION_PLANNER_INSTRUCTION,
    tools=[
        search_operational_incident_memory,
        purge_cloudflare_product_cache,
        create_shopify_recovery_discount,
    ],
    output_key="remediation_plan",
)

daily_reconciliation_pipeline = SequentialAgent(
    name="daily_reconciliation_pipeline",
    description=(
        "Full end-to-end store health and daily reconciliation pipeline: runs parallel "
        "Cloudflare + Shopify diagnostics followed by strategic remediation planning."
    ),
    sub_agents=[parallel_store_diagnostics, remediation_planner],
)

# 3. Strategic Root Coordinator (`gemini-2.5-pro`)
# Create dedicated sub-agent instances for targeted single-domain routing so ADK's
# single-parent agent tree invariant is preserved cleanly.
targeted_edge_specialist = LlmAgent(
    name="targeted_edge_specialist",
    model=_SETTINGS.specialist_model,
    description=(
        "Handles targeted questions purely about Cloudflare storefront routes, cache status, "
        "or catalog parity without running a full order reconciliation."
    ),
    instruction=EDGE_INSPECTOR_INSTRUCTION,
    tools=[
        inspect_cloudflare_edge_routes,
        verify_shopify_storefront_catalog_parity,
    ],
)

targeted_order_specialist = LlmAgent(
    name="targeted_order_specialist",
    model=_SETTINGS.specialist_model,
    description=(
        "Handles targeted questions purely about Shopify order margins, unconfirmed "
        "Printful fulfillment drafts, or past incident memory."
    ),
    instruction=ORDER_RECONCILER_INSTRUCTION,
    tools=[
        fetch_redacted_shopify_orders,
        reconcile_order_margins_and_fulfillment_sla,
        search_operational_incident_memory,
    ],
)

targeted_remediation_gate = LlmAgent(
    name="targeted_remediation_gate",
    model=_SETTINGS.coordinator_model,
    description=(
        "Handles approved or requested high-stakes remediations (Cloudflare product route "
        "cache purge or capped <=20% Shopify customer recovery discount creation) with HITL."
    ),
    instruction=REMEDIATION_PLANNER_INSTRUCTION,
    tools=[
        purge_cloudflare_product_cache,
        create_shopify_recovery_discount,
        search_operational_incident_memory,
    ],
)

root_agent = LlmAgent(
    name="commerce_ops_coordinator",
    model=_SETTINGS.coordinator_model,
    description=(
        "Root Alotta Cake Commerce Operations & Edge Reconciliation Coordinator routing "
        "across full parallel store diagnostics, targeted specialists, and HITL remediations."
    ),
    instruction=(
        f"{AGENT_CONSTITUTION}\n\n"
        "## Routing Rules\n"
        "- For full daily store checks or health + order reconciliations, delegate to "
        "`daily_reconciliation_pipeline`.\n"
        "- For edge/cache/catalog-only questions, delegate to `targeted_edge_specialist`.\n"
        "- For order/margin/SLA/history-only questions, delegate to `targeted_order_specialist`.\n"
        "- For remediation execution or HITL approvals, delegate to `targeted_remediation_gate`."
    ),
    sub_agents=[
        daily_reconciliation_pipeline,
        targeted_edge_specialist,
        targeted_order_specialist,
        targeted_remediation_gate,
    ],
)

# 4. Resumable App with History Compaction, Context Caching & Governance Guardrails
app = App(
    name="commerce_ops_agent",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=6,
        overlap_size=2,
    ),
    context_cache_config=ContextCacheConfig(
        min_tokens=4096,
        ttl_seconds=1800,
    ),
    plugins=[
        ContextFilterPlugin(num_invocations_to_keep=8),
        CommerceGovernancePlugin(),
    ],
)
