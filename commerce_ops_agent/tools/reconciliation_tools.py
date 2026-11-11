"""PII-Redacted Shopify Order Fetch, Deterministic Margin Math & Memory Lookup Tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ValidationError

from ..adapters.mode_router import BACKEND_ROUTER
from ..config import get_settings
from ..memory.session_store import DEFAULT_INCIDENT_STORE
from ..observability.telemetry import traced_operation
from ..schemas.commerce_models import (
    GuidedToolError,
    OrderAnomalyFinding,
    OrderFetchInput,
    OrderFetchOutput,
    ReconciliationInput,
    ReconciliationOutput,
    RedactedOrderRecord,
)
from ..security.pii_redactor import DEFAULT_PII_REDACTOR, pseudonymize_order_id


@traced_operation("tool.fetch_redacted_shopify_orders")
async def fetch_redacted_shopify_orders(
    lookback_hours: int = 72,
    status_filter: Literal["any", "unfulfilled", "paid"] = "any",
) -> dict[str, Any]:
    """Fetches recent Shopify orders and scrubs all customer PII in process memory.

    Converts raw order IDs into deterministic one-way HMAC pseudonyms (`ord_<12hex>`),
    strips buyer names, emails, phone numbers, and street addresses via `PiiRedactor`,
    and expresses all currency fields in integer USD minor units (cents).

    Args:
        lookback_hours: Hours of order history to inspect (1 to 720, default 72).
        status_filter: Filter orders by `"any"`, `"unfulfilled"`, or `"paid"`.

    Returns:
        Strict JSON dictionary matching `OrderFetchOutput` containing PII-free
        `RedactedOrderRecord` items, or `GuidedToolError` with recovery instructions.
    """
    try:
        validated_in = OrderFetchInput(
            lookback_hours=lookback_hours,
            status_filter=status_filter,
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_ORDER_FETCH_ARGUMENTS",
            message=str(exc),
            recovery_instruction=(
                "Pass `lookback_hours` between 1 and 720 and `status_filter` as one of "
                "`'any'`, `'unfulfilled'`, or `'paid'`."
            ),
        ).model_dump()

    try:
        raw_orders = await BACKEND_ROUTER.get_raw_orders(
            validated_in.lookback_hours,
            validated_in.status_filter,
        )
        redacted_records: list[RedactedOrderRecord] = []
        for raw in raw_orders:
            # Actively scrub raw payload before projection
            _ = DEFAULT_PII_REDACTOR.redact_payload(raw)
            pseudonym = pseudonymize_order_id(str(raw["raw_order_id"]))
            subtotal = int(raw["subtotal_cents"])
            shipping_charged = int(raw["shipping_charged_cents"])
            discount = int(raw["discount_cents"])
            net_rev = max(0, subtotal + shipping_charged - discount)

            redacted_records.append(
                RedactedOrderRecord(
                    order_pseudonym=pseudonym,
                    subtotal_cents=subtotal,
                    shipping_charged_cents=shipping_charged,
                    discount_cents=discount,
                    net_revenue_cents=net_rev,
                    estimated_pod_production_cents=int(raw["estimated_pod_production_cents"]),
                    estimated_pod_shipping_cents=int(raw["estimated_pod_shipping_cents"]),
                    fulfillment_draft_status=raw["fulfillment_draft_status"],
                    hours_since_paid=float(raw["hours_since_paid"]),
                    sku_handles=list(raw["sku_handles"]),
                )
            )

        return OrderFetchOutput(
            lookback_hours=validated_in.lookback_hours,
            order_count=len(redacted_records),
            orders=redacted_records,
        ).model_dump()
    except Exception as exc:
        return GuidedToolError(
            error_code="SHOPIFY_ORDER_FETCH_FAILED",
            message=DEFAULT_PII_REDACTOR.redact_text(str(exc)),
            recovery_instruction=(
                "Verify that the Shopify Dev Dashboard app has `read_orders` scope installed "
                "on `m7m2bn-sv.myshopify.com`, or narrow `lookback_hours` to 24."
            ),
        ).model_dump()


@traced_operation("tool.reconcile_order_margins_and_fulfillment_sla")
async def reconcile_order_margins_and_fulfillment_sla(
    min_margin_bps: int | None = None,
    fulfillment_sla_hours: int | None = None,
    lookback_hours: int = 72,
) -> dict[str, Any]:
    """Computes exact minor-unit contribution margins and flags fulfillment SLA breaches.

    Executes deterministic integer arithmetic (cents and basis points) across recent
    PII-redacted orders so the LLM never approximates unit economics.

    Args:
        min_margin_bps: Minimum pre-ad contribution margin in basis points
            (`3500` = `35.00%`). Defaults to `MIN_CONTRIBUTION_MARGIN_BPS` from config.
        fulfillment_sla_hours: Maximum hours an order may remain in `unconfirmed_draft`
            before triggering an SLA breach (default 24 hours).
        lookback_hours: Hours of order history to reconcile (1 to 720, default 72).

    Returns:
        Strict JSON dictionary matching `ReconciliationOutput` detailing aggregate
        economics and per-order anomalies, or `GuidedToolError` on invalid parameters.
    """
    settings = get_settings()
    try:
        validated_in = ReconciliationInput(
            min_margin_bps=(settings.min_contribution_margin_bps if min_margin_bps is None else min_margin_bps),
            fulfillment_sla_hours=(
                settings.fulfillment_draft_sla_hours if fulfillment_sla_hours is None else fulfillment_sla_hours
            ),
            lookback_hours=lookback_hours,
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_RECONCILIATION_PARAMETERS",
            message=str(exc),
            recovery_instruction=(
                "Use `min_margin_bps` between 0 and 9500 (e.g., 3500 for 35%) and "
                "`fulfillment_sla_hours` between 1 and 168."
            ),
        ).model_dump()

    fetched = await fetch_redacted_shopify_orders(
        lookback_hours=validated_in.lookback_hours,
        status_filter="any",
    )
    if fetched.get("status") == "error":
        return fetched

    orders = fetched.get("orders", [])
    agg_rev_cents = 0
    agg_margin_cents = 0
    margin_breaches = 0
    sla_breaches = 0
    anomalies: list[OrderAnomalyFinding] = []

    for order in orders:
        net_rev = int(order["net_revenue_cents"])
        pod_cost = int(order["estimated_pod_production_cents"]) + int(order["estimated_pod_shipping_cents"])
        margin_cents = net_rev - pod_cost
        margin_bps = round((margin_cents * 10000) / net_rev) if net_rev > 0 else 0

        agg_rev_cents += net_rev
        agg_margin_cents += margin_cents

        is_margin_breach = margin_bps < validated_in.min_margin_bps
        is_sla_breach = (
            order["fulfillment_draft_status"] == "unconfirmed_draft"
            and float(order["hours_since_paid"]) > validated_in.fulfillment_sla_hours
        )
        if is_margin_breach:
            margin_breaches += 1
        if is_sla_breach:
            sla_breaches += 1

        if is_margin_breach or is_sla_breach:
            if is_sla_breach and is_margin_breach:
                rec = (
                    "Review Printful draft manually before confirming production and offer a "
                    "capped <=15% customer recovery discount for the fulfillment delay."
                )
            elif is_sla_breach:
                rec = (
                    "Approve Printful fulfillment draft in operator console and consider a "
                    "capped <=15% recovery discount via `create_shopify_recovery_discount`."
                )
            else:
                rec = "Audit free-shipping / promo stacking rules; pre-ad contribution margin fell below policy floor."

            anomalies.append(
                OrderAnomalyFinding(
                    order_pseudonym=order["order_pseudonym"],
                    net_revenue_cents=net_rev,
                    total_fulfillment_cost_cents=pod_cost,
                    contribution_margin_cents=margin_cents,
                    contribution_margin_bps=margin_bps,
                    margin_breach=is_margin_breach,
                    sla_breach=is_sla_breach,
                    hours_in_draft=float(order["hours_since_paid"]),
                    recommended_action=rec,
                )
            )

    agg_bps = round((agg_margin_cents * 10000) / agg_rev_cents) if agg_rev_cents > 0 else 0
    return ReconciliationOutput(
        evaluated_orders=len(orders),
        aggregate_net_revenue_cents=agg_rev_cents,
        aggregate_margin_cents=agg_margin_cents,
        aggregate_margin_bps=agg_bps,
        margin_breach_count=margin_breaches,
        sla_breach_count=sla_breaches,
        anomalies=anomalies,
    ).model_dump()


@traced_operation("tool.search_operational_incident_memory")
async def search_operational_incident_memory(query: str = "", limit: int = 5) -> dict[str, Any]:
    """Searches persistent SQLite operational memory for past PII-redacted incidents.

    Enables multi-session continuity by retrieving previously recorded catalog drift,
    margin breaches, and delayed `ord_<hex>` pseudonyms.

    Args:
        query: Optional keyword, product handle, or `ord_<hex>` pseudonym to match.
        limit: Maximum number of historical incident summaries to return (1 to 20).

    Returns:
        Dictionary with `status="success"` and matching `incidents` records.
    """
    bounded_limit = max(1, min(20, int(limit)))
    incidents = DEFAULT_INCIDENT_STORE.search_recent_incidents(query=query, limit=bounded_limit)
    return {
        "status": "success",
        "query": query,
        "count": len(incidents),
        "incidents": incidents,
    }
