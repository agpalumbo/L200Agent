"""Public-safe synthetic fixtures modeled on Alotta Cake's Signature Line.

Used in `COMMERCE_MODE=fixture` for deterministic offline unit tests, GitHub Actions
CI, and `adk eval` regression suites without requiring live credentials or exposing
any production-derived records.
"""

from __future__ import annotations

from typing import Any

SYNTHETIC_EDGE_ROUTES: dict[str, dict[str, Any]] = {
    "/": {
        "http_status": 200,
        "cache_status": "HIT",
        "robots_noindex": True,
        "checkout_host_verified": True,
        "stale_cache_suspected": False,
        "latency_ms": 38.4,
    },
    "/products": {
        "http_status": 200,
        "cache_status": "HIT",
        "robots_noindex": True,
        "checkout_host_verified": True,
        "stale_cache_suspected": False,
        "latency_ms": 44.1,
    },
    "/products/signature-heavyweight-tee": {
        "http_status": 200,
        "cache_status": "HIT",
        "robots_noindex": True,
        "checkout_host_verified": True,
        "stale_cache_suspected": False,
        "latency_ms": 41.2,
    },
    "/products/signature-oversized-hoodie": {
        "http_status": 200,
        "cache_status": "STALE",
        "robots_noindex": True,
        "checkout_host_verified": True,
        "stale_cache_suspected": True,
        "latency_ms": 62.7,
    },
    "/studio": {
        "http_status": 200,
        "cache_status": "DYNAMIC",
        "robots_noindex": True,
        "checkout_host_verified": True,
        "stale_cache_suspected": False,
        "latency_ms": 51.0,
    },
}

SYNTHETIC_CATALOG: dict[str, dict[str, Any]] = {
    "signature-heavyweight-tee": {
        "title": "Signature Heavyweight Tee",
        "shopify_price_cents": 4200,
        "edge_rendered_price_cents": 4200,
        "shopify_active": True,
        "edge_visible": True,
    },
    "signature-oversized-hoodie": {
        "title": "Signature Oversized Hoodie",
        "shopify_price_cents": 8800,
        "edge_rendered_price_cents": 8400,
        "shopify_active": True,
        "edge_visible": True,
    },
    "signature-open-hem-sweatpants": {
        "title": "Signature Open-Hem Sweatpants",
        "shopify_price_cents": 9200,
        "edge_rendered_price_cents": 9200,
        "shopify_active": True,
        "edge_visible": True,
    },
    "signature-crew-socks": {
        "title": "Signature Crew Socks",
        "shopify_price_cents": 3400,
        "edge_rendered_price_cents": 3400,
        "shopify_active": True,
        "edge_visible": True,
    },
}

# Raw synthetic orders intentionally include fabricated PII to verify that
# `PiiRedactor` strips every customer/address field before LLM exposure.
SYNTHETIC_RAW_ORDERS: list[dict[str, Any]] = [
    {
        "raw_order_id": "9001002001",
        "customer": {
            "name": "Jordan Synthetic-Buyer",
            "email": "jordan.buyer@example.org",
            "phone": "415-555-0192",
        },
        "shipping_address": "742 Evergreen Terrace, San Francisco, CA",
        "subtotal_cents": 8800,
        "shipping_charged_cents": 695,
        "discount_cents": 0,
        "estimated_pod_production_cents": 3450,
        "estimated_pod_shipping_cents": 695,
        "fulfillment_draft_status": "confirmed_in_production",
        "hours_since_paid": 8.5,
        "sku_handles": ["signature-oversized-hoodie"],
    },
    {
        "raw_order_id": "9001002002",
        "customer": {
            "name": "Taylor Fabricated-Test",
            "email": "taylor.test@example.org",
            "phone": "510-555-0148",
        },
        "shipping_address": "1200 Broadway Ave, Oakland, CA",
        "subtotal_cents": 8400,
        "shipping_charged_cents": 0,
        "discount_cents": 1260,
        "estimated_pod_production_cents": 4650,
        "estimated_pod_shipping_cents": 995,
        "fulfillment_draft_status": "unconfirmed_draft",
        "hours_since_paid": 38.0,
        "sku_handles": ["signature-heavyweight-tee", "signature-crew-socks"],
    },
    {
        "raw_order_id": "9001002003",
        "customer": {
            "name": "Morgan Mock-Customer",
            "email": "morgan.mock@example.org",
            "phone": "650-555-0110",
        },
        "shipping_address": "500 El Camino Rd, Palo Alto, CA",
        "subtotal_cents": 9200,
        "shipping_charged_cents": 695,
        "discount_cents": 0,
        "estimated_pod_production_cents": 3800,
        "estimated_pod_shipping_cents": 695,
        "fulfillment_draft_status": "shipped",
        "hours_since_paid": 52.0,
        "sku_handles": ["signature-open-hem-sweatpants"],
    },
]
