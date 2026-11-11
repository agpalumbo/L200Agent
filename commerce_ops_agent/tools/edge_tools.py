"""Cloudflare Edge Health & Shopify Storefront Catalog Parity Tools."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from ..adapters.mode_router import BACKEND_ROUTER
from ..config import get_settings
from ..observability.telemetry import traced_operation
from ..schemas.commerce_models import (
    CatalogParityInput,
    CatalogParityOutput,
    EdgeInspectionInput,
    EdgeInspectionOutput,
    GuidedToolError,
    ProductParityItem,
    RouteHealthRecord,
)
from ..security.pii_redactor import DEFAULT_PII_REDACTOR


@traced_operation("tool.inspect_cloudflare_edge_routes")
async def inspect_cloudflare_edge_routes(
    routes: list[str] | None = None,
    verify_checkout_redirect_host: bool = True,
) -> dict[str, Any]:
    """Inspects live Cloudflare Worker storefront routes for HTTP health and stale cache.

    Probes the requested relative storefront paths on `https://alottacake.com`,
    inspecting HTTP status codes, `cf-cache-status` headers, `noindex` safeguards,
    and verifying that checkout links point strictly to `checkout.alottacake.com`.

    Args:
        routes: Optional list of relative storefront paths starting with `/`
            (for example `["/", "/products", "/products/signature-oversized-hoodie"]`).
            Defaults to the core Signature Line and Custom Studio routes when omitted.
        verify_checkout_redirect_host: When True, checks that the root storefront
            references the canonical branded Shopify checkout host.

    Returns:
        Strict JSON dictionary matching `EdgeInspectionOutput` on success, or
        `GuidedToolError` with a concrete `recovery_instruction` if validation
        or upstream network calls fail.
    """
    try:
        validated_in = EdgeInspectionInput(
            routes=routes
            or [
                "/",
                "/products",
                "/products/signature-heavyweight-tee",
                "/products/signature-oversized-hoodie",
                "/studio",
            ],
            verify_checkout_redirect_host=verify_checkout_redirect_host,
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_EDGE_ROUTE_ARGUMENTS",
            message=str(exc),
            recovery_instruction=(
                "Pass between 1 and 20 relative paths starting with `/` "
                "(e.g., `['/products/signature-heavyweight-tee']`), never full URLs."
            ),
        ).model_dump()

    settings = get_settings()
    try:
        raw_records = await BACKEND_ROUTER.get_edge_routes(
            validated_in.routes,
            validated_in.verify_checkout_redirect_host,
        )
        typed_records = [RouteHealthRecord(**rec) for rec in raw_records]
        stale_routes = [rec.route for rec in typed_records if rec.stale_cache_suspected]
        return EdgeInspectionOutput(
            zone_id=settings.cloudflare_zone_id,
            storefront_base_url=settings.cloudflare_storefront_base_url,
            inspected_routes=typed_records,
            stale_routes=stale_routes,
        ).model_dump()
    except httpx.HTTPStatusError as exc:
        return GuidedToolError(
            error_code=f"CLOUDFLARE_HTTP_{exc.response.status_code}",
            message=f"Edge probe returned HTTP {exc.response.status_code}.",
            recovery_instruction=(
                "Check if Cloudflare bot-fight or rate limiting is active on the route, "
                "narrow `routes` to `/products/<handle>`, and report degraded edge visibility."
            ),
        ).model_dump()
    except Exception as exc:
        return GuidedToolError(
            error_code="CLOUDFLARE_INSPECTION_UNAVAILABLE",
            message=DEFAULT_PII_REDACTOR.redact_text(str(exc)),
            recovery_instruction=(
                "Verify `CLOUDFLARE_STOREFRONT_BASE_URL` connectivity or continue the "
                "Shopify catalog check independently via `verify_shopify_storefront_catalog_parity`."
            ),
        ).model_dump()


@traced_operation("tool.verify_shopify_storefront_catalog_parity")
async def verify_shopify_storefront_catalog_parity(
    product_handles: list[str] | None = None,
) -> dict[str, Any]:
    """Compares authoritative Shopify product prices and status against Cloudflare SSR output.

    Detects price mismatches (in integer USD cents) or visibility drift between
    Shopify and the edge-cached storefront (`alottacake.com`).

    Args:
        product_handles: Optional list of Shopify product URL handles to verify
            (defaults to the 4 flagship Signature Line products when omitted).

    Returns:
        Strict JSON dictionary matching `CatalogParityOutput` listing all checked
        products and any `drifted_handles`, or `GuidedToolError` with recovery steps.
    """
    try:
        validated_in = CatalogParityInput(
            product_handles=product_handles
            or [
                "signature-heavyweight-tee",
                "signature-oversized-hoodie",
                "signature-open-hem-sweatpants",
                "signature-crew-socks",
            ]
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_PRODUCT_HANDLES",
            message=str(exc),
            recovery_instruction=(
                "Provide 1 to 25 lowercase product URL slugs such as "
                "`['signature-heavyweight-tee', 'signature-oversized-hoodie']`."
            ),
        ).model_dump()

    settings = get_settings()
    try:
        raw_items = await BACKEND_ROUTER.get_catalog_comparison(validated_in.product_handles)
        parity_items: list[ProductParityItem] = []
        drifted: list[str] = []

        for item in raw_items:
            price_ok = item["shopify_price_cents"] == item["edge_rendered_price_cents"]
            vis_ok = item["shopify_active"] == item["edge_visible"]
            matched = price_ok and vis_ok
            reason: str | None = None
            if not vis_ok:
                reason = (
                    f"Visibility mismatch: Shopify active={item['shopify_active']} "
                    f"vs Edge visible={item['edge_visible']}"
                )
            elif not price_ok:
                reason = (
                    f"Price drift: Shopify is {item['shopify_price_cents']} cents "
                    f"while Edge renders {item['edge_rendered_price_cents']} cents"
                )
            if not matched:
                drifted.append(item["handle"])

            parity_items.append(
                ProductParityItem(
                    handle=item["handle"],
                    title=item["title"],
                    shopify_price_cents=item["shopify_price_cents"],
                    edge_rendered_price_cents=item["edge_rendered_price_cents"],
                    shopify_active=item["shopify_active"],
                    edge_visible=item["edge_visible"],
                    parity_match=matched,
                    drift_reason=reason,
                )
            )

        return CatalogParityOutput(
            shop_domain=settings.shopify_shop_domain,
            checked_count=len(parity_items),
            drifted_handles=drifted,
            items=parity_items,
        ).model_dump()
    except Exception as exc:
        return GuidedToolError(
            error_code="CATALOG_PARITY_CHECK_FAILED",
            message=DEFAULT_PII_REDACTOR.redact_text(str(exc)),
            recovery_instruction=(
                "Confirm Shopify `read_products` scope is active on the configured "
                "Dev Dashboard credentials, or retry with a smaller `product_handles` subset."
            ),
        ).model_dump()
