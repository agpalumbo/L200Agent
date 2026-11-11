"""Secure runtime configuration and Google Cloud Secret Manager loader.

All sensitive credentials (Shopify OAuth / Admin tokens, Cloudflare API tokens,
and HMAC pseudonymization salts) are retrieved dynamically from Google Cloud
Secret Manager in cloud environments or from git-ignored local environment
variables during local development. Zero secrets are hardcoded in source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from google.cloud import secretmanager


def _load_optional_env_file(env_path: Path) -> None:
    """Loads key=value pairs from a local git-ignored file if present."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), cleaned)


class SecretVaultClient:
    """Fetches secrets from Google Cloud Secret Manager with safe local fallback."""

    def __init__(self, project_id: str, enabled: bool = False) -> None:
        self.project_id = project_id
        self.enabled = enabled and bool(project_id)
        self._client: secretmanager.SecretManagerServiceClient | None = None

    def _get_client(self) -> secretmanager.SecretManagerServiceClient:
        if self._client is None:
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def resolve_secret(self, secret_id: str, default: str = "") -> str:
        """Resolves `secret_id` from GCP Secret Manager first, then environment.

        Args:
            secret_id: Secret identifier in Google Cloud Secret Manager and env.
            default: Non-sensitive fallback when neither source defines a value.

        Returns:
            Resolved secret string without logging or persisting its value.
        """
        if self.enabled:
            try:
                name = f"projects/{self.project_id}/secrets/{secret_id}/versions/latest"
                response = self._get_client().access_secret_version(request={"name": name})
                payload = response.payload.data.decode("utf-8").strip()
                if payload:
                    return payload
            except Exception:
                # Fall back cleanly to environment injection if Secret Manager is unreachable
                pass
        env_val = os.getenv(secret_id, "").strip()
        if env_val and not env_val.startswith("replace-with-"):
            return env_val
        return default


@dataclass(frozen=True)
class AgentSettings:
    """Immutable runtime settings for the Commerce Operations Agent."""

    gcp_project_id: str
    gcp_location: str
    commerce_mode: Literal["fixture", "live"]
    shopify_shop_domain: str
    shopify_api_version: str
    shopify_checkout_hostname: str
    shopify_client_id: str = field(repr=False)
    shopify_client_secret: str = field(repr=False)
    shopify_admin_access_token: str = field(repr=False)
    cloudflare_storefront_base_url: str
    cloudflare_zone_id: str
    cloudflare_api_token: str = field(repr=False)
    commerce_hmac_salt: str = field(repr=False)
    min_contribution_margin_bps: int = 3500
    fulfillment_draft_sla_hours: int = 24
    max_recovery_discount_percent: int = 20
    enable_cloud_dlp: bool = False
    session_db_url: str = "sqlite+aiosqlite:///./.adk_sessions.db"
    coordinator_model: str = "gemini-2.5-pro"
    specialist_model: str = "gemini-2.5-flash"


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """Loads validated `AgentSettings` from Secret Manager and environment."""
    # Support optional local private env files outside Git
    _load_optional_env_file(Path.home() / ".config" / "l200-live.env")
    _load_optional_env_file(Path.home() / ".config" / "l200-shopify.env")
    _load_optional_env_file(Path(__file__).resolve().parent / ".env")
    _load_optional_env_file(Path(__file__).resolve().parent.parent / ".env")

    gcp_project_id = os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT", "l200-agpalumbo-et3xgz")
    use_sm = os.getenv("ENABLE_SECRET_MANAGER", "false").lower() in {"1", "true", "yes"}
    vault = SecretVaultClient(project_id=gcp_project_id, enabled=use_sm)

    raw_mode = os.getenv("COMMERCE_MODE", "fixture").strip().lower()
    mode: Literal["fixture", "live"] = "live" if raw_mode == "live" else "fixture"

    return AgentSettings(
        gcp_project_id=gcp_project_id,
        gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        commerce_mode=mode,
        shopify_shop_domain=os.getenv("SHOPIFY_SHOP_DOMAIN", "m7m2bn-sv.myshopify.com"),
        shopify_api_version=os.getenv("SHOPIFY_API_VERSION", "2026-07"),
        shopify_checkout_hostname=os.getenv("SHOPIFY_CHECKOUT_HOSTNAME", "checkout.alottacake.com"),
        shopify_client_id=vault.resolve_secret("SHOPIFY_CLIENT_ID"),
        shopify_client_secret=vault.resolve_secret("SHOPIFY_CLIENT_SECRET"),
        shopify_admin_access_token=vault.resolve_secret("SHOPIFY_ADMIN_ACCESS_TOKEN"),
        cloudflare_storefront_base_url=os.getenv("CLOUDFLARE_STOREFRONT_BASE_URL", "https://alottacake.com").rstrip(
            "/"
        ),
        cloudflare_zone_id=os.getenv("CLOUDFLARE_ZONE_ID", "098b4805a1195d34a58b8320e76872eb"),
        cloudflare_api_token=vault.resolve_secret("CLOUDFLARE_API_TOKEN"),
        commerce_hmac_salt=vault.resolve_secret("COMMERCE_HMAC_SALT", default="alottacake-public-safe-fixture-salt-v1"),
        min_contribution_margin_bps=int(os.getenv("MIN_CONTRIBUTION_MARGIN_BPS", "3500")),
        fulfillment_draft_sla_hours=int(os.getenv("FULFILLMENT_DRAFT_SLA_HOURS", "24")),
        max_recovery_discount_percent=int(os.getenv("MAX_RECOVERY_DISCOUNT_PERCENT", "20")),
        enable_cloud_dlp=os.getenv("ENABLE_CLOUD_DLP", "false").lower() in {"1", "true", "yes"},
        session_db_url=os.getenv("SESSION_DB_URL", "sqlite+aiosqlite:///./.adk_sessions.db"),
        coordinator_model=os.getenv("COORDINATOR_MODEL", "gemini-2.5-pro"),
        specialist_model=os.getenv("SPECIALIST_MODEL", "gemini-2.5-flash"),
    )
