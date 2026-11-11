"""Strict Pydantic v2 JSON schemas constraining LLM tool inputs and outputs."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SAFE_ROUTE_RE = re.compile(r"^/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?$")
_SAFE_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_SAFE_PRODUCT_PATH_RE = re.compile(r"^/products/[a-z0-9][a-z0-9-]{0,80}$")


class StrictSchema(BaseModel):
    """Base model enforcing strict JSON validation with zero unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GuidedToolError(StrictSchema):
    """Structured error response providing actionable LLM recovery instructions."""

    status: Literal["error"] = "error"
    error_code: str = Field(..., description="Machine-readable error classification.")
    message: str = Field(..., description="Sanitized explanation of why the call failed.")
    recovery_instruction: str = Field(
        ...,
        description="Step-by-step guidance telling the LLM how to recover or adjust.",
    )


# ------------------------------------------------------------------------------
# 1. Edge & Catalog Parity Schemas
# ------------------------------------------------------------------------------


class EdgeInspectionInput(StrictSchema):
    """Input schema for `inspect_cloudflare_edge_routes`."""

    routes: list[str] = Field(
        default_factory=lambda: [
            "/",
            "/products",
            "/products/signature-heavyweight-tee",
            "/products/signature-oversized-hoodie",
            "/studio",
        ],
        min_length=1,
        max_length=20,
        description="Relative storefront paths on `alottacake.com` to inspect.",
    )
    verify_checkout_redirect_host: bool = Field(
        default=True,
        description="Whether to validate that checkout links point to checkout.alottacake.com.",
    )

    @field_validator("routes")
    @classmethod
    def _validate_relative_routes(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for route in value:
            r = route.strip()
            if not r.startswith("/") or r.startswith("//") or ".." in r or not _SAFE_ROUTE_RE.match(r):
                raise ValueError(
                    f"Route '{r}' must be a clean relative path starting with '/' without '..' or query params."
                )
            cleaned.append(r)
        return cleaned


class RouteHealthRecord(StrictSchema):
    """Telemetry for a single Cloudflare Worker storefront route."""

    route: str
    url: str
    http_status: int
    cache_status: str
    robots_noindex: bool
    checkout_host_verified: bool
    stale_cache_suspected: bool
    latency_ms: float


class EdgeInspectionOutput(StrictSchema):
    """Validated output from `inspect_cloudflare_edge_routes`."""

    status: Literal["success"] = "success"
    zone_id: str
    storefront_base_url: str
    inspected_routes: list[RouteHealthRecord]
    stale_routes: list[str]


class CatalogParityInput(StrictSchema):
    """Input schema for `verify_shopify_storefront_catalog_parity`."""

    product_handles: list[str] = Field(
        default_factory=lambda: [
            "signature-heavyweight-tee",
            "signature-oversized-hoodie",
            "signature-open-hem-sweatpants",
            "signature-crew-socks",
        ],
        min_length=1,
        max_length=25,
        description="Product URL handles to cross-check between Shopify and Cloudflare edge.",
    )

    @field_validator("product_handles")
    @classmethod
    def _validate_handles(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for handle in value:
            h = handle.strip()
            if not _SAFE_HANDLE_RE.match(h):
                raise ValueError(f"Invalid product handle '{h}': must match lowercase slug `[a-z0-9-]+`.")
            cleaned.append(h)
        return cleaned


class ProductParityItem(StrictSchema):
    """Comparison record between authoritative Shopify catalog and Cloudflare edge."""

    handle: str
    title: str
    shopify_price_cents: int = Field(..., ge=0)
    edge_rendered_price_cents: int = Field(..., ge=0)
    shopify_active: bool
    edge_visible: bool
    parity_match: bool
    drift_reason: str | None = None


class CatalogParityOutput(StrictSchema):
    """Validated output from `verify_shopify_storefront_catalog_parity`."""

    status: Literal["success"] = "success"
    shop_domain: str
    checked_count: int
    drifted_handles: list[str]
    items: list[ProductParityItem]


# ------------------------------------------------------------------------------
# 2. Redacted Order & Margin Reconciliation Schemas
# ------------------------------------------------------------------------------


class OrderFetchInput(StrictSchema):
    """Input schema for `fetch_redacted_shopify_orders`."""

    lookback_hours: int = Field(
        default=72,
        ge=1,
        le=720,
        description="Lookback window in hours for recent Shopify orders.",
    )
    status_filter: Literal["any", "unfulfilled", "paid"] = Field(
        default="any",
        description="Order fulfillment/payment filter.",
    )


class RedactedOrderRecord(StrictSchema):
    """PII-free order projection using HMAC pseudonyms and integer minor units."""

    order_pseudonym: str = Field(..., pattern=r"^ord_[0-9a-f]{12}$")
    currency: Literal["USD"] = "USD"
    subtotal_cents: int = Field(..., ge=0)
    shipping_charged_cents: int = Field(..., ge=0)
    discount_cents: int = Field(..., ge=0)
    net_revenue_cents: int = Field(..., ge=0)
    estimated_pod_production_cents: int = Field(..., ge=0)
    estimated_pod_shipping_cents: int = Field(..., ge=0)
    fulfillment_draft_status: Literal["unconfirmed_draft", "confirmed_in_production", "shipped"]
    hours_since_paid: float = Field(..., ge=0.0)
    sku_handles: list[str]
    pii_redacted: Literal[True] = True


class OrderFetchOutput(StrictSchema):
    """Validated output from `fetch_redacted_shopify_orders`."""

    status: Literal["success"] = "success"
    lookback_hours: int
    order_count: int
    orders: list[RedactedOrderRecord]


class ReconciliationInput(StrictSchema):
    """Input schema for `reconcile_order_margins_and_fulfillment_sla`."""

    min_margin_bps: int = Field(
        default=3500,
        ge=0,
        le=9500,
        description="Minimum acceptable pre-ad contribution margin in basis points (3500 = 35%).",
    )
    fulfillment_sla_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Maximum hours an order may remain in unconfirmed fulfillment draft state.",
    )
    lookback_hours: int = Field(
        default=72,
        ge=1,
        le=720,
        description="Lookback window in hours matching the order fetch.",
    )


class OrderAnomalyFinding(StrictSchema):
    """Deterministic unit-economics and SLA finding for a single order."""

    order_pseudonym: str
    net_revenue_cents: int
    total_fulfillment_cost_cents: int
    contribution_margin_cents: int
    contribution_margin_bps: int
    margin_breach: bool
    sla_breach: bool
    hours_in_draft: float
    recommended_action: str


class ReconciliationOutput(StrictSchema):
    """Validated output from `reconcile_order_margins_and_fulfillment_sla`."""

    status: Literal["success"] = "success"
    evaluated_orders: int
    aggregate_net_revenue_cents: int
    aggregate_margin_cents: int
    aggregate_margin_bps: int
    margin_breach_count: int
    sla_breach_count: int
    anomalies: list[OrderAnomalyFinding]


# ------------------------------------------------------------------------------
# 3. High-Stakes Remediation Schemas (HITL Gated)
# ------------------------------------------------------------------------------


class CachePurgeInput(StrictSchema):
    """Input schema for `purge_cloudflare_product_cache`."""

    product_urls: list[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Exact https://alottacake.com/products/<handle> URLs to purge.",
    )
    reason: str = Field(..., min_length=5, max_length=300)
    operator_confirmed: bool = Field(
        default=False,
        description="Must be True ONLY after explicit human operator approval.",
    )

    @field_validator("product_urls")
    @classmethod
    def _validate_purge_urls(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw_url in value:
            u = raw_url.strip()
            parsed = urlparse(u)
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or ".." in parsed.path
                or not _SAFE_PRODUCT_PATH_RE.match(parsed.path)
            ):
                raise ValueError(
                    f"URL '{u}' must be a clean https://<host>/products/<handle> URL without '..' or query params."
                )
            cleaned.append(u)
        return cleaned


class CachePurgeOutput(StrictSchema):
    """Validated output from `purge_cloudflare_product_cache`."""

    status: Literal["success", "awaiting_human_confirmation"]
    purged_urls: list[str]
    cloudflare_purge_id: str | None = None
    confirmation_prompt: str | None = None


class RecoveryDiscountInput(StrictSchema):
    """Input schema for `create_shopify_recovery_discount`."""

    order_pseudonym: str = Field(..., pattern=r"^ord_[0-9a-f]{12}$")
    discount_percent: int = Field(
        ...,
        ge=1,
        le=50,
        description="Requested single-use recovery discount percentage (policy cap: 20%).",
    )
    expires_in_days: int = Field(default=14, ge=1, le=60)
    reason: str = Field(..., min_length=5, max_length=300)
    operator_confirmed: bool = Field(
        default=False,
        description="Must be True ONLY after explicit human operator approval.",
    )


class RecoveryDiscountOutput(StrictSchema):
    """Validated output from `create_shopify_recovery_discount`."""

    status: Literal["success", "awaiting_human_confirmation"]
    order_pseudonym: str
    discount_code: str | None = None
    discount_percent: int
    shopify_price_rule_id: str | None = None
    confirmation_prompt: str | None = None
