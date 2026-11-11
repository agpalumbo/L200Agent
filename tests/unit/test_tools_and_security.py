"""Unit tests for strict schemas, PII redaction, minor-unit math, HITL, and OTel."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from commerce_ops_agent.observability.structured_logger import (
    clear_audit_events,
    get_audit_events,
    record_intent_event,
    record_outcome_event,
)
from commerce_ops_agent.observability.telemetry import (
    clear_recorded_spans,
    get_recorded_spans,
)
from commerce_ops_agent.schemas.commerce_models import EdgeInspectionInput
from commerce_ops_agent.security.pii_redactor import (
    DEFAULT_PII_REDACTOR,
    pseudonymize_order_id,
)
from commerce_ops_agent.tools import (
    create_shopify_recovery_discount,
    fetch_redacted_shopify_orders,
    inspect_cloudflare_edge_routes,
    purge_cloudflare_product_cache,
    reconcile_order_margins_and_fulfillment_sla,
    verify_shopify_storefront_catalog_parity,
)


def test_strict_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EdgeInspectionInput(routes=["/products"], unexpected_extra_field=True)  # type: ignore[call-arg]


def test_pii_redactor_scrubs_text_and_payloads() -> None:
    raw = {
        "id": "9001002002",
        "email": "alice@example.com",
        "shipping_address": "742 Evergreen Terrace",
        "note": "Call me at 415-555-0199 or email bob@alottacake.org at 100 Main St",
    }
    scrubbed = DEFAULT_PII_REDACTOR.redact_payload(raw)
    assert scrubbed["id"].startswith("ord_")
    assert len(scrubbed["id"]) == 16
    assert scrubbed["email"] == "[REDACTED_PII]"
    assert scrubbed["shipping_address"] == "[REDACTED_PII]"
    assert "415-555-0199" not in scrubbed["note"]
    assert "bob@alottacake.org" not in scrubbed["note"]
    assert "100 Main St" not in scrubbed["note"]


@pytest.mark.asyncio
async def test_guided_error_recovery_on_bad_route() -> None:
    res = await inspect_cloudflare_edge_routes(routes=["https://bad-absolute-route"])
    assert res["status"] == "error"
    assert res["error_code"] == "INVALID_EDGE_ROUTE_ARGUMENTS"
    assert "starting with `/`" in res["recovery_instruction"]


@pytest.mark.asyncio
async def test_edge_and_catalog_parity_detects_hoodie_drift() -> None:
    clear_recorded_spans()
    edge = await inspect_cloudflare_edge_routes()
    assert edge["status"] == "success"
    assert "/products/signature-oversized-hoodie" in edge["stale_routes"]

    parity = await verify_shopify_storefront_catalog_parity()
    assert parity["status"] == "success"
    assert "signature-oversized-hoodie" in parity["drifted_handles"]

    spans = [s.name for s in get_recorded_spans()]
    assert "tool.inspect_cloudflare_edge_routes" in spans
    assert "tool.verify_shopify_storefront_catalog_parity" in spans


@pytest.mark.asyncio
async def test_order_reconciliation_redacts_pii_and_flags_breaches() -> None:
    orders_out = await fetch_redacted_shopify_orders(lookback_hours=72)
    assert orders_out["status"] == "success"
    assert orders_out["order_count"] == 3
    serialized = str(orders_out)
    assert "example.org" not in serialized
    assert "Evergreen" not in serialized

    recon = await reconcile_order_margins_and_fulfillment_sla(
        min_margin_bps=3500,
        fulfillment_sla_hours=24,
    )
    assert recon["status"] == "success"
    assert recon["sla_breach_count"] == 1
    assert recon["margin_breach_count"] == 1
    flagged = recon["anomalies"][0]
    assert flagged["order_pseudonym"] == pseudonymize_order_id("9001002002")
    assert flagged["sla_breach"] is True
    assert flagged["margin_breach"] is True


@pytest.mark.asyncio
async def test_hitl_confirmation_and_policy_caps_on_mutations() -> None:
    mock_ctx = MagicMock()
    mock_ctx.tool_confirmation = None

    # 1. Unconfirmed call triggers ADK request_confirmation and pauses
    paused = await purge_cloudflare_product_cache(
        product_urls=["https://alottacake.com/products/signature-oversized-hoodie"],
        reason="Stale edge price detected on hoodie route",
        operator_confirmed=False,
        tool_context=mock_ctx,
    )
    assert paused["status"] == "awaiting_human_confirmation"
    mock_ctx.request_confirmation.assert_called_once()

    # 2. Confirmed call executes cleanly
    approved_ctx = MagicMock()
    approved_ctx.tool_confirmation = MagicMock(confirmed=True)
    executed = await purge_cloudflare_product_cache(
        product_urls=["https://alottacake.com/products/signature-oversized-hoodie"],
        reason="Stale edge price detected on hoodie route",
        tool_context=approved_ctx,
    )
    assert executed["status"] == "success"
    assert executed["cloudflare_purge_id"] is not None

    # 3. Policy cap rejects >20% discount
    over_cap = await create_shopify_recovery_discount(
        order_pseudonym=pseudonymize_order_id("9001002002"),
        discount_percent=35,
        reason="Delayed fulfillment apology",
        operator_confirmed=True,
    )
    assert over_cap["status"] == "error"
    assert over_cap["error_code"] == "DISCOUNT_EXCEEDS_POLICY_CAP"


def test_intent_and_outcome_audit_logging() -> None:
    clear_audit_events()
    record_intent_event(
        actor="remediation_planner",
        tool_name="purge_cloudflare_product_cache",
        sanitized_args={"product_urls": ["https://alottacake.com/products/signature-oversized-hoodie"]},
        invocation_id="inv_001",
    )
    record_outcome_event(
        actor="remediation_planner",
        tool_name="purge_cloudflare_product_cache",
        outcome_status="success",
        sanitized_summary={"status": "success"},
        latency_ms=14.2,
        invocation_id="inv_001",
    )
    events = get_audit_events()
    assert len(events) == 2
    assert events[0]["audit_phase"] == "TOOL_INTENT"
    assert events[1]["audit_phase"] == "TOOL_OUTCOME"


@pytest.mark.asyncio
async def test_ssrf_path_traversal_and_secret_token_scrubbing() -> None:
    # Block path traversal and protocol-relative routes
    traversal = await inspect_cloudflare_edge_routes(routes=["/products/../../admin"])
    assert traversal["status"] == "error"
    proto_rel = await inspect_cloudflare_edge_routes(routes=["//evil.example.com/steal"])
    assert proto_rel["status"] == "error"

    # Block handle path traversal
    bad_handle = await verify_shopify_storefront_catalog_parity(product_handles=["../secret"])
    assert bad_handle["status"] == "error"

    # Block purge path traversal bypass
    bad_purge = await purge_cloudflare_product_cache(
        product_urls=["https://alottacake.com/products/../"],
        reason="Traversal attempt",
        operator_confirmed=True,
    )
    assert bad_purge["status"] == "error"

    # Scrub credential tokens from error strings
    leaked = DEFAULT_PII_REDACTOR.redact_text("Auth failed for Bearer cfut_1234567890abcdef and shpss_abcdef1234567890")
    assert "cfut_" not in leaked
    assert "shpss_" not in leaked
    assert "[REDACTED_SECRET]" in leaked
