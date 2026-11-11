"""Dual-mode router (`fixture` vs `live`) decoupling tools from network state."""

from __future__ import annotations

import secrets
from typing import Any

from ..config import get_settings
from .cloudflare_client import CloudflareEdgeAdapter
from .shopify_client import ShopifyAdminAdapter
from .synthetic_store_data import (
    SYNTHETIC_CATALOG,
    SYNTHETIC_EDGE_ROUTES,
    SYNTHETIC_RAW_ORDERS,
)


class CommerceBackendRouter:
    """Routes read/write adapter operations to `fixture` or `live` implementations."""

    def __init__(self) -> None:
        self.shopify = ShopifyAdminAdapter()
        self.cloudflare = CloudflareEdgeAdapter()

    @property
    def is_live(self) -> bool:
        return get_settings().commerce_mode == "live"

    async def get_edge_routes(self, routes: list[str], verify_checkout_host: bool) -> list[dict[str, Any]]:
        settings = get_settings()
        if self.is_live:
            return await self.cloudflare.inspect_routes(routes, verify_checkout_host)

        records: list[dict[str, Any]] = []
        for route in routes:
            preset = SYNTHETIC_EDGE_ROUTES.get(
                route,
                {
                    "http_status": 200,
                    "cache_status": "MISS",
                    "robots_noindex": True,
                    "checkout_host_verified": True,
                    "stale_cache_suspected": False,
                    "latency_ms": 49.0,
                },
            )
            records.append(
                {
                    "route": route,
                    "url": f"{settings.cloudflare_storefront_base_url}{route}",
                    **preset,
                }
            )
        return records

    async def get_catalog_comparison(self, handles: list[str]) -> list[dict[str, Any]]:
        if self.is_live:
            shopify_map = await self.shopify.fetch_products_by_handles(handles)
            items: list[dict[str, Any]] = []
            for handle in handles:
                shop_entry = shopify_map.get(handle)
                edge_entry = await self.cloudflare.fetch_edge_product_state(handle)
                if not shop_entry:
                    items.append(
                        {
                            "handle": handle,
                            "title": handle,
                            "shopify_price_cents": 0,
                            "edge_rendered_price_cents": edge_entry["edge_rendered_price_cents"],
                            "shopify_active": False,
                            "edge_visible": edge_entry["edge_visible"],
                        }
                    )
                else:
                    items.append(
                        {
                            "handle": handle,
                            "title": shop_entry["title"],
                            "shopify_price_cents": shop_entry["shopify_price_cents"],
                            "edge_rendered_price_cents": edge_entry["edge_rendered_price_cents"],
                            "shopify_active": shop_entry["shopify_active"],
                            "edge_visible": edge_entry["edge_visible"],
                        }
                    )
            return items

        results: list[dict[str, Any]] = []
        for handle in handles:
            entry = SYNTHETIC_CATALOG.get(
                handle,
                {
                    "title": handle.replace("-", " ").title(),
                    "shopify_price_cents": 4200,
                    "edge_rendered_price_cents": 4200,
                    "shopify_active": True,
                    "edge_visible": True,
                },
            )
            results.append({"handle": handle, **entry})
        return results

    async def get_raw_orders(self, lookback_hours: int, status_filter: str) -> list[dict[str, Any]]:
        if self.is_live:
            return await self.shopify.fetch_recent_orders(lookback_hours, status_filter)

        filtered: list[dict[str, Any]] = []
        for order in SYNTHETIC_RAW_ORDERS:
            if order["hours_since_paid"] > lookback_hours:
                continue
            if status_filter == "unfulfilled" and order["fulfillment_draft_status"] == "shipped":
                continue
            filtered.append(dict(order))
        return filtered

    async def purge_product_urls(self, urls: list[str]) -> str:
        if self.is_live:
            return await self.cloudflare.purge_urls(urls)
        return f"cf_sim_purge_{secrets.token_hex(4)}"

    async def create_recovery_discount(
        self,
        *,
        order_pseudonym: str,
        discount_percent: int,
        expires_in_days: int,
    ) -> tuple[str, str]:
        if self.is_live:
            return await self.shopify.create_single_use_discount(
                order_pseudonym=order_pseudonym,
                discount_percent=discount_percent,
                expires_in_days=expires_in_days,
            )
        code = f"CARE-{order_pseudonym[-6:].upper()}-SIM"
        return code, f"pr_sim_{secrets.token_hex(4)}"


BACKEND_ROUTER = CommerceBackendRouter()
