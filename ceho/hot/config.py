"""Runtime configuration for the CEHO hot server.

Process settings and the direct-source pipeline format are separate types
in this module. Process settings come from the environment. The pipeline
format is the PCM contract for one direct source.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv


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


def _s3_uri(name: str, environment: Mapping[str, str]) -> str:
    value = _required_value(name, environment)
    parsed = urlparse(value)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an s3://bucket URI")
    return value


def _optional_value(name: str, environment: Mapping[str, str]) -> str | None:
    value = environment.get(name, "").strip()
    return value or None


def _optional_http_url(name: str, environment: Mapping[str, str]) -> str | None:
    value = environment.get(name, "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an absolute http(s) URI")
    return value


def _region_value(environment: Mapping[str, str]) -> str:
    value = environment.get("CEHO_S3_REGION", "us-east-1").strip()
    if not value:
        raise ConfigurationError("CEHO_S3_REGION must be configured")
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
class DirectSourcePipelineConfig:
    """PCM format and provider-failure bound for one direct-source STT run."""

    sample_rate: int = 16_000
    sample_width: int = 2
    channels: int = 1
    max_provider_failures: int = 3

    def frame_bytes(self, frame_ms: int) -> int:
        """Return the PCM size of one frame at this format."""
        return self.sample_rate * frame_ms // 1000 * self.sample_width * self.channels


DIRECT_SOURCE_PIPELINE = DirectSourcePipelineConfig()


@dataclass(frozen=True)
class HotServerSettings:
    """Validated configuration needed before the hot server can start."""

    database_url: str
    media_storage_uri: str
    s3_endpoint_url: str | None
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str
    signaling_secret: str
    event_publisher_url: str
    host: str
    port: int
    deepgram_api_key: str | None = None

    @property
    def s3_use_path_style(self) -> bool:
        """Custom endpoints, including local MinIO, use path-style S3 addressing."""
        return self.s3_endpoint_url is not None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> HotServerSettings:
        """Build settings from the process environment.

        With no mapping, load ``.env`` from the working directory and then read
        the process environment. Names already set in the process stay in place.
        An explicit mapping is used as given.
        """
        if environment is None:
            load_dotenv()
            source: Mapping[str, str] = os.environ
        else:
            source = environment
        return cls(
            database_url=_url_value("DATABASE_URL", source),
            media_storage_uri=_s3_uri("CEHO_MEDIA_STORAGE_URI", source),
            s3_endpoint_url=_optional_http_url("CEHO_S3_ENDPOINT_URL", source),
            s3_access_key_id=_required_value("CEHO_S3_ACCESS_KEY_ID", source),
            s3_secret_access_key=_required_value("CEHO_S3_SECRET_ACCESS_KEY", source),
            s3_region=_region_value(source),
            signaling_secret=_required_value("CEHO_SIGNALING_SECRET", source),
            event_publisher_url=_url_value("CEHO_EVENT_PUBLISHER_URL", source),
            host=source.get("CEHO_HOST", "127.0.0.1"),
            port=_port_value(source),
            deepgram_api_key=_optional_value("DEEPGRAM_API_KEY", source),
        )
