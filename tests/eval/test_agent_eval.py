"""Automated Golden Dataset Evaluation Harness (`commerce_ops.evalset.json`).

Statically and behaviorally evaluates all 5 golden operational scenarios in
deterministic `fixture` mode so regressions in tool contracts, PII scrubbing,
minor-unit economics, HITL pauses, or policy guardrails fail CI immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.tools import FunctionTool
from google.genai import types

from commerce_ops_agent.security.guardrails_plugin import CommerceGovernancePlugin
from commerce_ops_agent.security.pii_redactor import pseudonymize_order_id
from commerce_ops_agent.tools import (
    create_shopify_recovery_discount,
    fetch_redacted_shopify_orders,
    inspect_cloudflare_edge_routes,
    purge_cloudflare_product_cache,
    reconcile_order_margins_and_fulfillment_sla,
    verify_shopify_storefront_catalog_parity,
)

EVALSET_PATH = Path(__file__).resolve().parent / "commerce_ops.evalset.json"


def load_evalset() -> dict:
    return json.loads(EVALSET_PATH.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_golden_evalset_end_to_end_regression() -> None:
    evalset = load_evalset()
    cases = {c["case_id"]: c for c in evalset["cases"]}
    assert len(cases) == 5

    # Case 1: Edge & Catalog Drift
    c1 = cases["golden_01_edge_and_catalog_drift"]
    edge_out = await inspect_cloudflare_edge_routes()
    parity_out = await verify_shopify_storefront_catalog_parity()
    assert c1["expected_stale_route"] in edge_out["stale_routes"]
    assert c1["expected_drifted_handle"] in parity_out["drifted_handles"]

    # Case 2: PII-Free Order Margin & SLA Reconciliation
    c2 = cases["golden_02_order_margin_and_sla_reconciliation"]
    orders_out = await fetch_redacted_shopify_orders()
    recon_out = await reconcile_order_margins_and_fulfillment_sla()
    assert recon_out["sla_breach_count"] == c2["expected_sla_breach_count"]
    assert recon_out["margin_breach_count"] == c2["expected_margin_breach_count"]
    combined_dump = json.dumps({"orders": orders_out, "recon": recon_out})
    for forbidden in c2["forbidden_substrings"]:
        assert forbidden not in combined_dump

    # Case 3: HITL Pause -> Operator Approval Resume
    c3 = cases["golden_03_hitl_pause_before_cache_purge"]
    unconfirmed_ctx = MagicMock(tool_confirmation=None)
    paused = await purge_cloudflare_product_cache(
        product_urls=[c3["target_url"]],
        reason="Golden eval stale route remediation",
        tool_context=unconfirmed_ctx,
    )
    assert paused["status"] == c3["expected_unconfirmed_status"]
    unconfirmed_ctx.request_confirmation.assert_called_once()

    confirmed_ctx = MagicMock(tool_confirmation=MagicMock(confirmed=True))
    resumed = await purge_cloudflare_product_cache(
        product_urls=[c3["target_url"]],
        reason="Golden eval stale route remediation",
        tool_context=confirmed_ctx,
    )
    assert resumed["status"] == c3["expected_confirmed_status"]

    # Case 4: Policy Guardrail Plugin Blocks >20% Discount
    c4 = cases["golden_04_policy_cap_blocks_excessive_discount"]
    plugin = CommerceGovernancePlugin()
    discount_tool = FunctionTool(create_shopify_recovery_discount)
    guard_res = await plugin.before_tool_callback(
        tool=discount_tool,
        tool_args={
            "order_pseudonym": pseudonymize_order_id("9001002002"),
            "discount_percent": c4["requested_discount_percent"],
            "reason": "Delayed shipment",
        },
        tool_context=MagicMock(invocation_id="eval_04", agent_name="remediation_planner"),
    )
    assert guard_res is not None
    assert guard_res["error_code"] == c4["expected_error_code"]

    # Case 5: Prompt Injection & Root-Domain Purge Guard
    c5 = cases["golden_05_prompt_injection_and_zone_purge_guard"]
    inj_req = LlmRequest(contents=[types.Content(role="user", parts=[types.Part.from_text(text=c5["user_query"])])])
    inj_block = await plugin.before_model_callback(
        callback_context=MagicMock(),
        llm_request=inj_req,
    )
    assert inj_block is not None

    purge_tool = FunctionTool(purge_cloudflare_product_cache)
    scope_block = await plugin.before_tool_callback(
        tool=purge_tool,
        tool_args={"product_urls": [c5["invalid_purge_url"]], "reason": "Attempt root purge"},
        tool_context=MagicMock(invocation_id="eval_05", agent_name="remediation_planner"),
    )
    assert scope_block is not None
    assert scope_block["error_code"] == "POLICY_INVALID_PURGE_SCOPE"
