"""Scoped Shopify Admin client supporting Dev Dashboard 24h Client Credentials."""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..config import AgentSettings, get_settings
from ..observability.telemetry import traced_operation


class ShopifyAdminAdapter:
    """Bounded Shopify Admin API client with automatic 24h OAuth token refresh."""

    def __init__(self, settings: AgentSettings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cached_token: str = ""
        self._token_expires_at: float = 0.0

    @property
    def base_admin_url(self) -> str:
        return f"https://{self.settings.shopify_shop_domain}/admin/api/{self.settings.shopify_api_version}"

    @traced_operation("adapter.shopify.get_access_token")
    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        """Resolves a static Admin token or exchanges Dev Dashboard Client Credentials."""
        if self.settings.shopify_admin_access_token:
            return self.settings.shopify_admin_access_token

        now = time.time()
        if self._cached_token and now < (self._token_expires_at - 120):
            return self._cached_token

        if not (self.settings.shopify_client_id and self.settings.shopify_client_secret):
            raise RuntimeError(
                "Missing Shopify credentials: configure SHOPIFY_CLIENT_ID + "
                "SHOPIFY_CLIENT_SECRET (or SHOPIFY_ADMIN_ACCESS_TOKEN) via Secret Manager."
            )

        token_url = f"https://{self.settings.shopify_shop_domain}/admin/oauth/access_token"
        response = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.shopify_client_id,
                "client_secret": self.settings.shopify_client_secret,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            raise RuntimeError("Shopify OAuth endpoint returned an empty access_token.")
        expires_in = int(payload.get("expires_in", 86399))
        self._cached_token = access_token
        self._token_expires_at = now + expires_in
        return access_token

    @traced_operation("adapter.shopify.fetch_products_by_handles")
    async def fetch_products_by_handles(self, handles: list[str]) -> dict[str, dict[str, Any]]:
        """Fetches authoritative product status and lowest variant price in USD cents."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await self._get_access_token(client)
            resp = await client.get(
                f"{self.base_admin_url}/products.json",
                params={"handle": ",".join(handles), "limit": 50},
                headers={"X-Shopify-Access-Token": token, "Accept": "application/json"},
            )
            resp.raise_for_status()
            products = resp.json().get("products", [])

        mapped: dict[str, dict[str, Any]] = {}
        for prod in products:
            handle = str(prod.get("handle", ""))
            variants = prod.get("variants") or []
            prices_cents = [round(float(v.get("price", "0")) * 100) for v in variants if v.get("price") is not None]
            mapped[handle] = {
                "title": str(prod.get("title", handle)),
                "shopify_price_cents": min(prices_cents) if prices_cents else 0,
                "shopify_active": prod.get("status") == "active",
            }
        return mapped

    @traced_operation("adapter.shopify.fetch_recent_orders")
    async def fetch_recent_orders(self, lookback_hours: int, status_filter: str) -> list[dict[str, Any]]:
        """Fetches recent orders and extracts only financial/SLA fields in memory."""
        cutoff = (datetime.now(UTC) - timedelta(hours=lookback_hours)).isoformat()
        params: dict[str, Any] = {
            "status": "any",
            "created_at_min": cutoff,
            "limit": 50,
        }
        if status_filter == "unfulfilled":
            params["fulfillment_status"] = "unfulfilled"
        elif status_filter == "paid":
            params["financial_status"] = "paid"

        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await self._get_access_token(client)
            resp = await client.get(
                f"{self.base_admin_url}/orders.json",
                params=params,
                headers={"X-Shopify-Access-Token": token, "Accept": "application/json"},
            )
            resp.raise_for_status()
            raw_orders = resp.json().get("orders", [])

        normalized: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for order in raw_orders:
            subtotal_cents = round(float(order.get("subtotal_price", "0")) * 100)
            discounts_cents = round(float(order.get("total_discounts", "0")) * 100)
            shipping_lines = order.get("shipping_lines") or []
            shipping_cents = sum(round(float(s.get("price", "0")) * 100) for s in shipping_lines)
            created_raw = order.get("created_at")
            hours_elapsed = 1.0
            if created_raw:
                created_dt = datetime.fromisoformat(str(created_raw))
                hours_elapsed = max(0.1, round((now - created_dt).total_seconds() / 3600.0, 1))

            fulfillment_status = order.get("fulfillment_status")
            if fulfillment_status == "fulfilled":
                draft_state = "shipped"
            elif fulfillment_status == "partial":
                draft_state = "confirmed_in_production"
            else:
                draft_state = "unconfirmed_draft"

            line_items = order.get("line_items") or []
            sku_handles = [
                str(li.get("name") or li.get("sku") or "item").lower().replace(" ", "-") for li in line_items
            ]
            # Estimate baseline POD production at 45% of subtotal when external POD invoice is absent
            est_pod_cents = max(1200, round(subtotal_cents * 0.45))
            est_ship_cents = 695 if line_items else 0

            normalized.append(
                {
                    "raw_order_id": str(order.get("id", "")),
                    "customer": order.get("customer") or {},
                    "shipping_address": order.get("shipping_address") or {},
                    "subtotal_cents": subtotal_cents,
                    "shipping_charged_cents": shipping_cents,
                    "discount_cents": discounts_cents,
                    "estimated_pod_production_cents": est_pod_cents,
                    "estimated_pod_shipping_cents": est_ship_cents,
                    "fulfillment_draft_status": draft_state,
                    "hours_since_paid": hours_elapsed,
                    "sku_handles": sku_handles or ["signature-item"],
                }
            )
        return normalized

    @traced_operation("adapter.shopify.create_single_use_discount")
    async def create_single_use_discount(
        self,
        *,
        order_pseudonym: str,
        discount_percent: int,
        expires_in_days: int,
    ) -> tuple[str, str]:
        """Creates a single-use percentage recovery discount code in Shopify."""
        suffix = secrets.token_hex(3).upper()
        code = f"CARE-{order_pseudonym[-6:].upper()}-{suffix}"
        now = datetime.now(UTC)
        ends_at = (now + timedelta(days=expires_in_days)).isoformat()

        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await self._get_access_token(client)
            headers = {"X-Shopify-Access-Token": token, "Accept": "application/json"}
            rule_resp = await client.post(
                f"{self.base_admin_url}/price_rules.json",
                headers=headers,
                json={
                    "price_rule": {
                        "title": code,
                        "target_type": "line_item",
                        "target_selection": "all",
                        "allocation_method": "across",
                        "value_type": "percentage",
                        "value": f"-{discount_percent}.0",
                        "customer_selection": "all",
                        "usage_limit": 1,
                        "once_per_customer": True,
                        "starts_at": now.isoformat(),
                        "ends_at": ends_at,
                    }
                },
            )
            rule_resp.raise_for_status()
            rule_id = str(rule_resp.json()["price_rule"]["id"])

            code_resp = await client.post(
                f"{self.base_admin_url}/price_rules/{rule_id}/discount_codes.json",
                headers=headers,
                json={"discount_code": {"code": code}},
            )
            code_resp.raise_for_status()
        return code, rule_id
