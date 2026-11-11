"""Scoped Cloudflare Edge Storefront & Zone Cache Purge Adapter."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..config import AgentSettings, get_settings
from ..observability.telemetry import traced_operation

_PRICE_DOLLAR_RE = re.compile(r"\$(\d+(?:\.\d{2})?)")


class CloudflareEdgeAdapter:
    """Inspects live Cloudflare Worker SSR routes and executes URL-scoped purges."""

    def __init__(self, settings: AgentSettings | None = None) -> None:
        self.settings = settings or get_settings()

    @traced_operation("adapter.cloudflare.inspect_routes")
    async def inspect_routes(self, routes: list[str], verify_checkout_host: bool = True) -> list[dict[str, Any]]:
        """Probes storefront routes and checks cache headers + checkout host bindings."""
        results: list[dict[str, Any]] = []
        headers = {
            "User-Agent": "AlottaCake-CommerceOpsAgent/1.0 (+https://alottacake.com)",
            "Accept": "text/html,application/xhtml+xml",
        }
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
            for route in routes:
                url = f"{self.settings.cloudflare_storefront_base_url}{route}"
                started = time.perf_counter()
                resp = await client.get(url)
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
                body = resp.text or ""
                cf_cache = resp.headers.get("cf-cache-status", "DYNAMIC").upper()
                robots_hdr = resp.headers.get("x-robots-tag", "").lower()
                has_noindex = "noindex" in robots_hdr or 'content="noindex' in body.lower()
                checkout_ok = (
                    (self.settings.shopify_checkout_hostname in body)
                    if (verify_checkout_host and route == "/")
                    else True
                )
                stale = cf_cache in {"STALE", "EXPIRED", "REVALIDATED"}
                results.append(
                    {
                        "route": route,
                        "url": url,
                        "http_status": resp.status_code,
                        "cache_status": cf_cache,
                        "robots_noindex": has_noindex,
                        "checkout_host_verified": checkout_ok,
                        "stale_cache_suspected": stale,
                        "latency_ms": elapsed_ms,
                    }
                )
        return results

    @traced_operation("adapter.cloudflare.extract_edge_product_price")
    async def fetch_edge_product_state(self, handle: str) -> dict[str, Any]:
        """Fetches `/products/<handle>` on `alottacake.com` to read rendered price."""
        url = f"{self.settings.cloudflare_storefront_base_url}/products/{handle}"
        headers = {"User-Agent": "AlottaCake-CommerceOpsAgent/1.0"}
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"edge_visible": False, "edge_rendered_price_cents": 0}
            match = _PRICE_DOLLAR_RE.search(resp.text or "")
            cents = round(float(match.group(1)) * 100) if match else 0
            return {"edge_visible": True, "edge_rendered_price_cents": cents}

    @traced_operation("adapter.cloudflare.purge_urls")
    async def purge_urls(self, urls: list[str]) -> str:
        """Purges ONLY explicit `/products/*` URLs via Cloudflare's Zone Purge API."""
        if not self.settings.cloudflare_api_token:
            raise RuntimeError("Missing CLOUDFLARE_API_TOKEN: inject via Secret Manager or local env.")
        endpoint = f"https://api.cloudflare.com/client/v4/zones/{self.settings.cloudflare_zone_id}/purge_cache"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self.settings.cloudflare_api_token}",
                    "Content-Type": "application/json",
                },
                json={"files": urls},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Cloudflare purge API reported errors: {data.get('errors')}")
            return str((data.get("result") or {}).get("id", "cf-purge-ok"))
