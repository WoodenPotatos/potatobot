"""Deployment-owned settings shared by the bot and dashboard.

Profiles change safe defaults and access policy; they must never select divergent
feature implementations or permit data sharing between installations.
"""

from dataclasses import dataclass
from enum import Enum
import os
from urllib.parse import urlparse


class DeploymentProfile(str, Enum):
    PRIVATE = "private"
    MANAGED = "managed"
    SELF_HOSTED = "self_hosted"


@dataclass(frozen=True)
class DeploymentSettings:
    profile: DeploymentProfile
    dashboard_enabled: bool
    dashboard_host: str
    dashboard_port: int
    dashboard_external_url: str | None
    discord_redirect_uri: str | None
    # Number of trusted reverse proxies in front of the dashboard. Only forwarded
    # headers from this many hops are honoured, so a client cannot spoof its own
    # address by sending X-Forwarded-For.
    trusted_proxy_hops: int


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def load_deployment_settings() -> DeploymentSettings:
    """Load and validate instance-level settings without reading guild config."""
    raw_profile = os.getenv("POTATOBOT_DEPLOYMENT_PROFILE", "private").strip().lower()
    try:
        profile = DeploymentProfile(raw_profile)
    except ValueError as exc:
        allowed = ", ".join(profile.value for profile in DeploymentProfile)
        raise ValueError(
            f"POTATOBOT_DEPLOYMENT_PROFILE must be one of: {allowed}"
        ) from exc

    default_dashboard_enabled = profile is DeploymentProfile.PRIVATE
    dashboard_enabled = _environment_bool(
        "POTATOBOT_DASHBOARD_ENABLED", default_dashboard_enabled
    )
    dashboard_host = os.getenv("POTATOBOT_DASHBOARD_HOST", "127.0.0.1").strip()
    if not dashboard_host:
        raise ValueError("POTATOBOT_DASHBOARD_HOST cannot be empty")
    try:
        dashboard_port = int(os.getenv("POTATOBOT_DASHBOARD_PORT", "5000"))
    except ValueError as exc:
        raise ValueError("POTATOBOT_DASHBOARD_PORT must be an integer") from exc
    if not 1 <= dashboard_port <= 65535:
        raise ValueError("POTATOBOT_DASHBOARD_PORT must be between 1 and 65535")

    external_url = os.getenv("POTATOBOT_DASHBOARD_EXTERNAL_URL", "").strip() or None
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "").strip() or None
    if external_url or redirect_uri:
        if not external_url or not redirect_uri:
            raise ValueError(
                "POTATOBOT_DASHBOARD_EXTERNAL_URL and DISCORD_REDIRECT_URI "
                "must be configured together"
            )
        external = urlparse(external_url)
        redirect = urlparse(redirect_uri)
        if external.scheme != "https" or redirect.scheme != "https":
            raise ValueError("Dashboard external and OAuth callback URLs must use HTTPS")
        if not external.netloc or not redirect.netloc:
            raise ValueError("Dashboard external and OAuth callback URLs need a host")
        if (external.scheme, external.netloc) != (redirect.scheme, redirect.netloc):
            raise ValueError("Dashboard external and OAuth callback origins must match")
        if (
            redirect.path.rstrip("/") != "/api/callback"
            or redirect.params
            or redirect.query
            or redirect.fragment
        ):
            raise ValueError("DISCORD_REDIRECT_URI must end with /api/callback")
        if (
            external.path not in {"", "/"}
            or external.params
            or external.query
            or external.fragment
        ):
            raise ValueError(
                "POTATOBOT_DASHBOARD_EXTERNAL_URL must be an origin without a path"
            )
        if dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "Externally proxied dashboards must bind to a loopback address"
            )
    # The documented deployment puts exactly one reverse proxy in front of the
    # dashboard, so a configured external URL implies one trusted hop. Without
    # this every client would share the loopback address as its rate-limit
    # identity. Operators with a different topology override the count.
    default_hops = 1 if external_url else 0
    configured_hops = os.getenv("POTATOBOT_TRUSTED_PROXY_HOPS", "").strip()
    if configured_hops:
        try:
            trusted_proxy_hops = int(configured_hops)
        except ValueError as exc:
            raise ValueError("POTATOBOT_TRUSTED_PROXY_HOPS must be an integer") from exc
        if not 0 <= trusted_proxy_hops <= 4:
            raise ValueError("POTATOBOT_TRUSTED_PROXY_HOPS must be between 0 and 4")
    else:
        trusted_proxy_hops = default_hops

    return DeploymentSettings(
        profile=profile,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        dashboard_external_url=external_url,
        discord_redirect_uri=redirect_uri,
        trusted_proxy_hops=trusted_proxy_hops,
    )


settings = load_deployment_settings()
