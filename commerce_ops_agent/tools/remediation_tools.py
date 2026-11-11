"""High-Stakes Remediation Tools Protected by ADK Human-in-the-Loop Confirmation."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from google.adk.tools import ToolContext
from pydantic import ValidationError

from ..adapters.mode_router import BACKEND_ROUTER
from ..config import get_settings
from ..observability.telemetry import traced_operation
from ..schemas.commerce_models import (
    CachePurgeInput,
    CachePurgeOutput,
    GuidedToolError,
    RecoveryDiscountInput,
    RecoveryDiscountOutput,
)
from ..security.pii_redactor import DEFAULT_PII_REDACTOR


def _is_human_confirmed(
    tool_context: ToolContext | None,
    operator_confirmed: bool,
) -> tuple[bool, bool]:
    """Checks ADK `tool_context.tool_confirmation` or explicit operator confirmation.

    Returns:
        `(confirmed, rejected)` tuple.
    """
    if tool_context is not None and getattr(tool_context, "tool_confirmation", None) is not None:
        conf = tool_context.tool_confirmation
        if getattr(conf, "confirmed", False):
            return True, False
        return False, True
    if operator_confirmed:
        return True, False
    return False, False


@traced_operation("tool.purge_cloudflare_product_cache")
async def purge_cloudflare_product_cache(
    product_urls: list[str],
    reason: str,
    operator_confirmed: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Purges specific `/products/<handle>` edge cache URLs on `alottacake.com` after HITL approval.

    HIGH-STAKES ACTION: Enforces a mandatory Human-in-the-Loop confirmation stop via
    `tool_context.request_confirmation(...)` before calling Cloudflare's Zone Purge API.
    Full-zone purges and non-product paths are strictly rejected.

    Args:
        product_urls: List of 1 to 10 exact `https://alottacake.com/products/<handle>` URLs.
        reason: Concise audit reason explaining why the cache purge is necessary.
        operator_confirmed: Set True ONLY after the human operator explicitly confirms.
        tool_context: Injected ADK `ToolContext` managing HITL confirmation state.

    Returns:
        Strict JSON dictionary matching `CachePurgeOutput` (`awaiting_human_confirmation`
        when paused for approval, or `success` once confirmed), or `GuidedToolError`.
    """
    try:
        validated_in = CachePurgeInput(
            product_urls=product_urls,
            reason=reason,
            operator_confirmed=operator_confirmed,
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_CACHE_PURGE_INPUT",
            message=str(exc),
            recovery_instruction=("Provide 1 to 10 exact product URLs and a `reason` of at least 5 characters."),
        ).model_dump()

    settings = get_settings()
    allowed_host = urlparse(settings.cloudflare_storefront_base_url).netloc
    for url in validated_in.product_urls:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != allowed_host or not parsed.path.startswith("/products/"):
            return GuidedToolError(
                error_code="FORBIDDEN_CACHE_PURGE_TARGET",
                message=f"Target '{url}' violates `/products/*` host allowlist.",
                recovery_instruction=(
                    f"Restrict `product_urls` strictly to `https://{allowed_host}/products/<handle>`."
                ),
            ).model_dump()

    confirmed, rejected = _is_human_confirmed(tool_context, validated_in.operator_confirmed)
    if rejected:
        return GuidedToolError(
            error_code="OPERATOR_REJECTED_CACHE_PURGE",
            message="Human operator declined the Cloudflare cache purge request.",
            recovery_instruction="Acknowledge the operator's rejection and do not retry the purge.",
        ).model_dump()

    if not confirmed:
        prompt = (
            f"Approval required to purge {len(validated_in.product_urls)} Cloudflare edge URL(s): "
            f"{', '.join(validated_in.product_urls)} (Reason: {validated_in.reason})."
        )
        if tool_context is not None:
            tool_context.request_confirmation(
                hint=prompt,
                payload={
                    "action": "purge_cloudflare_product_cache",
                    "product_urls": validated_in.product_urls,
                    "reason": validated_in.reason,
                },
            )
        return CachePurgeOutput(
            status="awaiting_human_confirmation",
            purged_urls=validated_in.product_urls,
            confirmation_prompt=prompt,
        ).model_dump()

    try:
        purge_id = await BACKEND_ROUTER.purge_product_urls(validated_in.product_urls)
        return CachePurgeOutput(
            status="success",
            purged_urls=validated_in.product_urls,
            cloudflare_purge_id=purge_id,
        ).model_dump()
    except Exception as exc:
        return GuidedToolError(
            error_code="CLOUDFLARE_PURGE_API_ERROR",
            message=DEFAULT_PII_REDACTOR.redact_text(str(exc)),
            recovery_instruction=(
                "Verify `CLOUDFLARE_API_TOKEN` has `Zone -> Cache Purge -> Purge` scope on "
                f"zone `{settings.cloudflare_zone_id}` before retrying."
            ),
        ).model_dump()


@traced_operation("tool.create_shopify_recovery_discount")
async def create_shopify_recovery_discount(
    order_pseudonym: str,
    discount_percent: int,
    reason: str,
    expires_in_days: int = 14,
    operator_confirmed: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Creates a single-use customer recovery discount code in Shopify after HITL approval.

    HIGH-STAKES ACTION: Enforces both the 20% maximum policy ceiling AND a mandatory
    Human-in-the-Loop stop (`tool_context.request_confirmation(...)`) before mutating
    Shopify price rules.

    Args:
        order_pseudonym: Pseudonymous order identifier (`ord_<12hex>`) impacted by SLA delay.
        discount_percent: Percentage off (1 to 20 maximum allowed by policy).
        reason: Concise audit justification for issuing the recovery code.
        expires_in_days: Days until the single-use discount expires (default 14).
        operator_confirmed: Set True ONLY after explicit human operator approval.
        tool_context: Injected ADK `ToolContext` managing HITL confirmation state.

    Returns:
        Strict JSON dictionary matching `RecoveryDiscountOutput` (`awaiting_human_confirmation`
        until approved, or `success` with generated `discount_code`), or `GuidedToolError`.
    """
    try:
        validated_in = RecoveryDiscountInput(
            order_pseudonym=order_pseudonym,
            discount_percent=discount_percent,
            expires_in_days=expires_in_days,
            reason=reason,
            operator_confirmed=operator_confirmed,
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_DISCOUNT_INPUT",
            message=str(exc),
            recovery_instruction=(
                "Ensure `order_pseudonym` matches `ord_<12hex>`, `discount_percent` is an "
                "integer <= 20, and `reason` explains the SLA breach."
            ),
        ).model_dump()

    settings = get_settings()
    if validated_in.discount_percent > settings.max_recovery_discount_percent:
        return GuidedToolError(
            error_code="DISCOUNT_EXCEEDS_POLICY_CAP",
            message=(
                f"Requested {validated_in.discount_percent}% exceeds the "
                f"{settings.max_recovery_discount_percent}% maximum cap."
            ),
            recovery_instruction=(f"Reduce `discount_percent` to {settings.max_recovery_discount_percent} or lower."),
        ).model_dump()

    confirmed, rejected = _is_human_confirmed(tool_context, validated_in.operator_confirmed)
    if rejected:
        return GuidedToolError(
            error_code="OPERATOR_REJECTED_RECOVERY_DISCOUNT",
            message="Human operator declined creation of the Shopify recovery discount.",
            recovery_instruction="Acknowledge the rejection and do not create a discount code.",
        ).model_dump()

    if not confirmed:
        prompt = (
            f"Approval required to create a single-use {validated_in.discount_percent}% Shopify "
            f"recovery discount for {validated_in.order_pseudonym} expiring in "
            f"{validated_in.expires_in_days} days (Reason: {validated_in.reason})."
        )
        if tool_context is not None:
            tool_context.request_confirmation(
                hint=prompt,
                payload={
                    "action": "create_shopify_recovery_discount",
                    "order_pseudonym": validated_in.order_pseudonym,
                    "discount_percent": validated_in.discount_percent,
                    "expires_in_days": validated_in.expires_in_days,
                    "reason": validated_in.reason,
                },
            )
        return RecoveryDiscountOutput(
            status="awaiting_human_confirmation",
            order_pseudonym=validated_in.order_pseudonym,
            discount_percent=validated_in.discount_percent,
            confirmation_prompt=prompt,
        ).model_dump()

    try:
        code, rule_id = await BACKEND_ROUTER.create_recovery_discount(
            order_pseudonym=validated_in.order_pseudonym,
            discount_percent=validated_in.discount_percent,
            expires_in_days=validated_in.expires_in_days,
        )
        return RecoveryDiscountOutput(
            status="success",
            order_pseudonym=validated_in.order_pseudonym,
            discount_code=code,
            discount_percent=validated_in.discount_percent,
            shopify_price_rule_id=rule_id,
        ).model_dump()
    except Exception as exc:
        return GuidedToolError(
            error_code="SHOPIFY_DISCOUNT_CREATION_FAILED",
            message=DEFAULT_PII_REDACTOR.redact_text(str(exc)),
            recovery_instruction=(
                "Verify `write_discounts` / price-rule permissions on the configured Shopify store before retrying."
            ),
        ).model_dump()
