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


def turn_credential_pair(key_id: str | None, api_token: str | None) -> tuple[str, str] | None:
    """Return a Cloudflare TURN key and token, or nothing when both are unset.

    One value without the other is a configuration error. The names in the
    message are the environment variables the operator sets.
    """
    key = (key_id or "").strip()
    token = (api_token or "").strip()
    if not key and not token:
        return None
    if not key:
        raise ConfigurationError("CEHO_TURN_KEY_ID must be configured")
    if not token:
        raise ConfigurationError("CEHO_TURN_API_TOKEN must be configured")
    return key, token


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


def _positive_seconds(name: str, environment: Mapping[str, str], default: int) -> int:
    raw = environment.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if value < 1:
        raise ConfigurationError(f"{name} must be at least 1")
    return value


def _ice_url(
    name: str,
    environment: Mapping[str, str],
    default: str,
    schemes: frozenset[str],
) -> str:
    value = environment.get(name, default).strip()
    parsed = urlparse(value)
    host = parsed.hostname or parsed.path.split("?", 1)[0].split("/")[0]
    if parsed.scheme not in schemes or not host:
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
class DirectSourcePipelineConfig:
    """PCM format, speech provider, and provider-failure bound for one direct source.

    ``stt_provider`` selects how a final result is read. ``deepgram`` is the
    live provider and is the only one whose word speaker numbers are split
    into separate candidates.
    """

    sample_rate: int = 16_000
    sample_width: int = 2
    channels: int = 1
    max_provider_failures: int = 3
    stt_provider: str = "deepgram"

    def __post_init__(self) -> None:
        provider = self.stt_provider.strip().lower()
        if not provider:
            raise ConfigurationError("stt_provider must be configured")
        object.__setattr__(self, "stt_provider", provider)

    def frame_bytes(self, frame_ms: int) -> int:
        """Return the PCM size of one frame at this format."""
        return self.sample_rate * frame_ms // 1000 * self.sample_width * self.channels


DIRECT_SOURCE_PIPELINE = DirectSourcePipelineConfig()


@dataclass(frozen=True)
class RecordingConfig:
    """Bounds for one direct-source recording queue and its S3 part size.

    The queue is in-process. ``part_bytes`` is the multipart part size. The
    S3 adapter rejects a part smaller than 5 MiB. Tests can use a smaller
    part with a fake store.
    """

    max_queued_bytes: int = 1024 * 1024
    max_queued_age_ms: int = 15_000
    part_bytes: int = 5 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_queued_bytes < 1:
            raise ConfigurationError("recording queue must hold at least one byte")
        if self.max_queued_age_ms < 1:
            raise ConfigurationError("recording queue age must be at least 1 ms")
        if self.part_bytes < 1:
            raise ConfigurationError("recording part size must be at least 1 byte")


@dataclass(frozen=True)
class TransportQueueConfig:
    """Bounds for one direct-source inbound media queue.

    The queue holds at most ``max_queued_frames`` frames and a frame may wait
    at most ``max_frame_age_ms``. Past either bound, the oldest frame is
    dropped and the loss is reported. A frame larger than ``max_frame_bytes``
    is not queued. The queue never grows without a bound.
    """

    max_queued_frames: int = 50
    max_frame_age_ms: int = 1_000
    max_frame_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if self.max_queued_frames < 1:
            raise ConfigurationError("transport queue must hold at least one frame")
        if self.max_frame_age_ms < 1:
            raise ConfigurationError("transport frame age must be at least 1 ms")
        if self.max_frame_bytes < 1:
            raise ConfigurationError("transport frame size must be at least 1 byte")


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
    control_secret: str
    event_publisher_url: str
    host: str
    port: int
    control_credential_ttl_seconds: int = 900
    signaling_credential_ttl_seconds: int = 60
    session_idle_timeout_seconds: int = 900
    stun_url: str = "stun:stun.cloudflare.com:3478"
    turn_url: str = "turns:turn.cloudflare.com:443?transport=tcp"
    turn_key_id: str | None = None
    turn_api_token: str | None = None
    turn_ttl_seconds: int = 60
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
        turn_key_id = _optional_value("CEHO_TURN_KEY_ID", source)
        turn_api_token = _optional_value("CEHO_TURN_API_TOKEN", source)
        turn_credential_pair(turn_key_id, turn_api_token)
        return cls(
            database_url=_url_value("DATABASE_URL", source),
            media_storage_uri=_s3_uri("CEHO_MEDIA_STORAGE_URI", source),
            s3_endpoint_url=_optional_http_url("CEHO_S3_ENDPOINT_URL", source),
            s3_access_key_id=_required_value("CEHO_S3_ACCESS_KEY_ID", source),
            s3_secret_access_key=_required_value("CEHO_S3_SECRET_ACCESS_KEY", source),
            s3_region=_region_value(source),
            signaling_secret=_required_value("CEHO_SIGNALING_SECRET", source),
            control_secret=_required_value("CEHO_CONTROL_SECRET", source),
            event_publisher_url=_url_value("CEHO_EVENT_PUBLISHER_URL", source),
            control_credential_ttl_seconds=_positive_seconds(
                "CEHO_CONTROL_CREDENTIAL_TTL_SECONDS", source, 900
            ),
            signaling_credential_ttl_seconds=_positive_seconds(
                "CEHO_SIGNALING_CREDENTIAL_TTL_SECONDS", source, 60
            ),
            session_idle_timeout_seconds=_positive_seconds(
                "CEHO_SESSION_IDLE_TIMEOUT_SECONDS", source, 900
            ),
            host=source.get("CEHO_HOST", "127.0.0.1"),
            port=_port_value(source),
            stun_url=_ice_url(
                "CEHO_STUN_URL",
                source,
                "stun:stun.cloudflare.com:3478",
                frozenset({"stun", "stuns"}),
            ),
            turn_url=_ice_url(
                "CEHO_TURN_URL",
                source,
                "turns:turn.cloudflare.com:443?transport=tcp",
                frozenset({"turn", "turns"}),
            ),
            turn_key_id=turn_key_id,
            turn_api_token=turn_api_token,
            turn_ttl_seconds=_positive_seconds("CEHO_TURN_TTL_SECONDS", source, 60),
            deepgram_api_key=_optional_value("DEEPGRAM_API_KEY", source),
        )
