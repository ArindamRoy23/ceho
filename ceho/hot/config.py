"""Runtime configuration for the CEHO hot server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when required hot-server configuration is absent or invalid."""


def _required_value(name: str, environment: Mapping[str, str]) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be configured")
    return value


def _url_value(name: str, environment: Mapping[str, str]) -> str:
    value = _required_value(name, environment)
    parsed = urlparse(value)
    if not parsed.scheme:
        raise ConfigurationError(f"{name} must be an absolute URI")
    return value


def _port_value(environment: Mapping[str, str]) -> int:
    value = environment.get("CEHO_PORT", "8000")
    try:
        port = int(value)
    except ValueError as error:
        raise ConfigurationError("CEHO_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ConfigurationError("CEHO_PORT must be between 1 and 65535")
    return port


@dataclass(frozen=True)
class HotServerSettings:
    """Validated configuration needed before the hot server can start."""

    database_url: str
    media_storage_uri: str
    signaling_secret: str
    event_publisher_url: str
    host: str
    port: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> HotServerSettings:
        """Build settings from the process environment."""
        source = os.environ if environment is None else environment
        return cls(
            database_url=_url_value("DATABASE_URL", source),
            media_storage_uri=_url_value("CEHO_MEDIA_STORAGE_URI", source),
            signaling_secret=_required_value("CEHO_SIGNALING_SECRET", source),
            event_publisher_url=_url_value("CEHO_EVENT_PUBLISHER_URL", source),
            host=source.get("CEHO_HOST", "127.0.0.1"),
            port=_port_value(source),
        )
