"""Active PII Redaction and HMAC Pseudonymization Pipeline.

Scrubs sensitive buyer data (emails, phone numbers, street addresses, payment card
patterns, and raw Shopify order IDs) BEFORE any payload enters LLM context, logs,
or persistent session memory. Integrates Google Cloud Sensitive Data Protection
(Cloud DLP API) when enabled, backed by a deterministic zero-latency local scrubber.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from google.cloud import dlp_v2

from ..config import get_settings
from ..observability.telemetry import traced_operation

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_SECRET_TOKEN_RE = re.compile(
    r"(?:\b(?:cfut|shpat|shpss|gho|ghp)_[A-Za-z0-9_-]{8,}\b"
    r"|\bAIza[0-9A-Za-z_-]{20,}\b"
    r"|Bearer\s+[A-Za-z0-9._-]{12,})"
)
_SHOPIFY_GID_RE = re.compile(r"gid://shopify/(?:Order|Customer|FulfillmentOrder)/(\d+)")
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.\s]{2,35}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way)\b\.?",
    re.IGNORECASE,
)

_SENSITIVE_DICT_KEYS = frozenset(
    {
        "email",
        "contact_email",
        "customer_email",
        "phone",
        "customer",
        "shipping_address",
        "billing_address",
        "first_name",
        "last_name",
        "name",
        "address1",
        "address2",
        "zip",
        "client_ip",
        "browser_ip",
        "credit_card",
        "tracking_number",
        "tracking_url",
    }
)


def pseudonymize_order_id(raw_order_id: str | int, salt: str | None = None) -> str:
    """Derives a deterministic one-way `ord_<12hex>` HMAC-SHA256 pseudonym."""
    secret_salt = (salt or get_settings().commerce_hmac_salt).encode("utf-8")
    normalized = str(raw_order_id).strip().encode("utf-8")
    digest = hmac.new(secret_salt, normalized, hashlib.sha256).hexdigest()[:12]
    return f"ord_{digest}"


class PiiRedactor:
    """Scrubs PII from free-form text and nested JSON payloads."""

    def __init__(self, project_id: str | None = None, enable_cloud_dlp: bool | None = None) -> None:
        settings = get_settings()
        self.project_id = project_id or settings.gcp_project_id
        self.enable_cloud_dlp = settings.enable_cloud_dlp if enable_cloud_dlp is None else enable_cloud_dlp
        self._dlp_client: dlp_v2.DlpServiceClient | None = None

    def _scrub_via_cloud_dlp(self, text: str) -> str:
        """Calls Google Cloud DLP `deidentify_content` when enabled in cloud mode."""
        if not self.enable_cloud_dlp or not self.project_id or not text:
            return text
        try:
            if self._dlp_client is None:
                self._dlp_client = dlp_v2.DlpServiceClient()
            parent = f"projects/{self.project_id}/locations/global"
            inspect_config = {
                "info_types": [
                    {"name": "EMAIL_ADDRESS"},
                    {"name": "PHONE_NUMBER"},
                    {"name": "PERSON_NAME"},
                    {"name": "STREET_ADDRESS"},
                    {"name": "CREDIT_CARD_NUMBER"},
                ]
            }
            deidentify_config = {
                "info_type_transformations": {
                    "transformations": [{"primitive_transformation": {"replace_with_info_type_config": {}}}]
                }
            }
            response = self._dlp_client.deidentify_content(
                request={
                    "parent": parent,
                    "inspect_config": inspect_config,
                    "deidentify_config": deidentify_config,
                    "item": {"value": text},
                }
            )
            return response.item.value
        except Exception:
            # Always fall through to deterministic local regex scrubbing
            return text

    @traced_operation("security.redact_text")
    def redact_text(self, text: str) -> str:
        """Scrubs emails, phone numbers, cards, street addresses, and raw Shopify GIDs."""
        if not text:
            return text
        scrubbed = self._scrub_via_cloud_dlp(text)
        scrubbed = _SECRET_TOKEN_RE.sub("[REDACTED_SECRET]", scrubbed)
        scrubbed = _EMAIL_RE.sub("[REDACTED_EMAIL]", scrubbed)
        scrubbed = _PHONE_RE.sub("[REDACTED_PHONE]", scrubbed)
        scrubbed = _CARD_RE.sub("[REDACTED_CARD]", scrubbed)
        scrubbed = _STREET_RE.sub("[REDACTED_ADDRESS]", scrubbed)
        scrubbed = _SHOPIFY_GID_RE.sub(
            lambda m: pseudonymize_order_id(m.group(1)),
            scrubbed,
        )
        return scrubbed

    @traced_operation("security.redact_payload")
    def redact_payload(self, payload: Any) -> Any:
        """Recursively scrubs sensitive dictionary keys and string values."""
        if isinstance(payload, str):
            return self.redact_text(payload)
        if isinstance(payload, list):
            return [self.redact_payload(item) for item in payload]
        if isinstance(payload, dict):
            sanitized: dict[str, Any] = {}
            for key, value in payload.items():
                lowered = str(key).lower()
                if lowered in _SENSITIVE_DICT_KEYS:
                    sanitized[key] = "[REDACTED_PII]"
                elif lowered in {"id", "order_id", "shopify_order_id"} and isinstance(value, (int, str)):
                    val_str = str(value)
                    if val_str.startswith("ord_"):
                        sanitized[key] = val_str
                    else:
                        sanitized[key] = pseudonymize_order_id(val_str)
                else:
                    sanitized[key] = self.redact_payload(value)
            return sanitized
        return payload


DEFAULT_PII_REDACTOR = PiiRedactor()
