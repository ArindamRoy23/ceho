"""Tests for hot-server startup configuration."""

import os
import unittest
from unittest.mock import patch

import pytest

from ceho.hot.config import (
    DIRECT_SOURCE_PIPELINE,
    ConfigurationError,
    DirectSourcePipelineConfig,
    HotServerSettings,
    RecordingConfig,
)


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "DATABASE_URL": "postgresql://ceho:ceho@localhost/ceho",
        "CEHO_MEDIA_STORAGE_URI": "s3://ceho-media",
        "CEHO_S3_ENDPOINT_URL": "http://minio:9000",
        "CEHO_S3_ACCESS_KEY_ID": "ceho",
        "CEHO_S3_SECRET_ACCESS_KEY": "ceho-dev-only",
        "CEHO_S3_REGION": "us-east-1",
        "CEHO_SIGNALING_SECRET": "test-secret",
        "CEHO_CONTROL_SECRET": "control-secret",
        "CEHO_EVENT_PUBLISHER_URL": "pgmq://ceho",
        "CEHO_TURN_KEY_ID": "turn-key",
        "CEHO_TURN_API_TOKEN": "turn-token",
    }
    values.update(overrides)
    return values


class HotServerSettingsTest(unittest.TestCase):
    """Verify required hot-server startup settings."""

    def test_missing_control_secret_fails_clearly(self) -> None:
        environment = _environment()
        del environment["CEHO_CONTROL_SECRET"]

        with self.assertRaisesRegex(ConfigurationError, "CEHO_CONTROL_SECRET must be configured"):
            HotServerSettings.from_environment(environment)

    def test_control_credential_ttl_defaults_to_fifteen_minutes(self) -> None:
        settings = HotServerSettings.from_environment(_environment())

        self.assertEqual(settings.control_credential_ttl_seconds, 900)

    def test_control_credential_ttl_must_be_an_integer(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError,
            "CEHO_CONTROL_CREDENTIAL_TTL_SECONDS must be an integer",
        ):
            HotServerSettings.from_environment(
                _environment(CEHO_CONTROL_CREDENTIAL_TTL_SECONDS="soon")
            )

    def test_control_credential_ttl_must_be_at_least_one_second(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError,
            "CEHO_CONTROL_CREDENTIAL_TTL_SECONDS must be at least 1",
        ):
            HotServerSettings.from_environment(
                _environment(CEHO_CONTROL_CREDENTIAL_TTL_SECONDS="0")
            )

    def test_missing_required_setting_fails_clearly(self) -> None:
        environment = _environment()
        del environment["CEHO_SIGNALING_SECRET"]

        with self.assertRaisesRegex(ConfigurationError, "CEHO_SIGNALING_SECRET"):
            HotServerSettings.from_environment(environment)

    def test_valid_environment_builds_settings(self) -> None:
        settings = HotServerSettings.from_environment(
            _environment(CEHO_HOST="0.0.0.0", CEHO_PORT="9000")
        )

        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.media_storage_uri, "s3://ceho-media")
        self.assertEqual(settings.s3_endpoint_url, "http://minio:9000")
        self.assertTrue(settings.s3_use_path_style)

    def test_media_storage_must_be_an_s3_bucket_uri(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "CEHO_MEDIA_STORAGE_URI"):
            HotServerSettings.from_environment(
                _environment(CEHO_MEDIA_STORAGE_URI="file:///tmp/ceho-media")
            )

    def test_unset_s3_endpoint_uses_cloud_addressing(self) -> None:
        environment = _environment()
        del environment["CEHO_S3_ENDPOINT_URL"]

        settings = HotServerSettings.from_environment(environment)

        self.assertIsNone(settings.s3_endpoint_url)
        self.assertFalse(settings.s3_use_path_style)

    def test_direct_source_pipeline_config_is_separate_from_process_settings(self) -> None:
        audio = DirectSourcePipelineConfig()

        self.assertEqual(audio.sample_rate, 16_000)
        self.assertEqual(audio.sample_width, 2)
        self.assertEqual(audio.channels, 1)
        self.assertEqual(audio.max_provider_failures, 3)
        self.assertEqual(audio.stt_provider, "deepgram")
        self.assertEqual(audio.frame_bytes(20), 640)
        self.assertEqual(DIRECT_SOURCE_PIPELINE, audio)

    def test_blank_stt_provider_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "stt_provider must be configured"):
            DirectSourcePipelineConfig(stt_provider="  ")

    def test_stt_provider_is_normalized(self) -> None:
        audio = DirectSourcePipelineConfig(stt_provider=" Deepgram ")

        self.assertEqual(audio.stt_provider, "deepgram")

    def test_recording_bounds_default_to_a_bounded_queue_and_five_mebibyte_parts(self) -> None:
        recording = RecordingConfig()

        self.assertEqual(recording.max_queued_bytes, 1024 * 1024)
        self.assertEqual(recording.max_queued_age_ms, 15_000)
        self.assertEqual(recording.part_bytes, 5 * 1024 * 1024)

    def test_recording_queue_must_hold_at_least_one_byte(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError, "recording queue must hold at least one byte"
        ):
            RecordingConfig(max_queued_bytes=0)

    def test_recording_queue_age_must_be_positive(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError, "recording queue age must be at least 1 ms"
        ):
            RecordingConfig(max_queued_age_ms=0)

    def test_recording_part_size_must_be_positive(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError, "recording part size must be at least 1 byte"
        ):
            RecordingConfig(part_bytes=0)

    def test_deepgram_api_key_is_optional(self) -> None:
        settings = HotServerSettings.from_environment(_environment())

        self.assertIsNone(settings.deepgram_api_key)

    def test_deepgram_api_key_is_read_when_present(self) -> None:
        settings = HotServerSettings.from_environment(_environment(DEEPGRAM_API_KEY="test-key"))

        self.assertEqual(settings.deepgram_api_key, "test-key")

    def test_blank_deepgram_api_key_is_absent(self) -> None:
        settings = HotServerSettings.from_environment(_environment(DEEPGRAM_API_KEY="  "))

        self.assertIsNone(settings.deepgram_api_key)

    def test_explicit_mapping_does_not_load_dotenv(self) -> None:
        with patch("ceho.hot.config.load_dotenv") as load_dotenv:
            with self.assertRaisesRegex(ConfigurationError, "CEHO_SIGNALING_SECRET"):
                environment = _environment()
                del environment["CEHO_SIGNALING_SECRET"]
                HotServerSettings.from_environment(environment)

        load_dotenv.assert_not_called()

    def test_process_environment_loads_dotenv(self) -> None:
        values = _environment(CEHO_SIGNALING_SECRET="from-dotenv")
        del values["CEHO_S3_ENDPOINT_URL"]

        def fill_environment() -> bool:
            os.environ.update(values)
            return True

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ceho.hot.config.load_dotenv", side_effect=fill_environment) as load_dotenv,
        ):
            settings = HotServerSettings.from_environment()

        load_dotenv.assert_called_once_with()
        self.assertEqual(settings.signaling_secret, "from-dotenv")


def test_signaling_ttl_and_idle_timeout_default() -> None:
    settings = HotServerSettings.from_environment(_environment())

    assert settings.signaling_credential_ttl_seconds == 60
    assert settings.session_idle_timeout_seconds == 900


def test_signaling_ttl_and_idle_timeout_are_read() -> None:
    settings = HotServerSettings.from_environment(
        _environment(
            CEHO_SIGNALING_CREDENTIAL_TTL_SECONDS="15",
            CEHO_SESSION_IDLE_TIMEOUT_SECONDS="120",
        )
    )

    assert settings.signaling_credential_ttl_seconds == 15
    assert settings.session_idle_timeout_seconds == 120


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CEHO_SIGNALING_CREDENTIAL_TTL_SECONDS", "0"),
        ("CEHO_SIGNALING_CREDENTIAL_TTL_SECONDS", "nope"),
        ("CEHO_SIGNALING_CREDENTIAL_TTL_SECONDS", ""),
        ("CEHO_SESSION_IDLE_TIMEOUT_SECONDS", "-1"),
        ("CEHO_SESSION_IDLE_TIMEOUT_SECONDS", "  "),
    ],
)
def test_session_duration_settings_fail_clearly(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        HotServerSettings.from_environment(_environment(**{name: value}))


def test_turn_credentials_are_optional() -> None:
    environment = _environment()
    del environment["CEHO_TURN_KEY_ID"]
    del environment["CEHO_TURN_API_TOKEN"]

    settings = HotServerSettings.from_environment(environment)

    assert settings.turn_key_id is None
    assert settings.turn_api_token is None


def test_cloudflare_ice_urls_default_and_turn_ttl_defaults_to_one_minute() -> None:
    settings = HotServerSettings.from_environment(
        _environment(CEHO_TURN_KEY_ID="key-1", CEHO_TURN_API_TOKEN="token-1")
    )

    assert settings.stun_url == "stun:stun.cloudflare.com:3478"
    assert settings.turn_url == "turns:turn.cloudflare.com:443?transport=tcp"
    assert settings.turn_key_id == "key-1"
    assert settings.turn_api_token == "token-1"
    assert settings.turn_ttl_seconds == 60


def test_ice_urls_and_turn_ttl_are_read_from_the_environment() -> None:
    settings = HotServerSettings.from_environment(
        _environment(
            CEHO_STUN_URL="stun:stun.example:3478",
            CEHO_TURN_URL="turn:turn.example:3478?transport=udp",
            CEHO_TURN_KEY_ID="key-2",
            CEHO_TURN_API_TOKEN="token-2",
            CEHO_TURN_TTL_SECONDS="120",
        )
    )

    assert settings.stun_url == "stun:stun.example:3478"
    assert settings.turn_url == "turn:turn.example:3478?transport=udp"
    assert settings.turn_ttl_seconds == 120


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CEHO_TURN_KEY_ID", ""),
        ("CEHO_TURN_API_TOKEN", "  "),
        ("CEHO_STUN_URL", "https://stun.cloudflare.com"),
        ("CEHO_TURN_URL", "stun:turn.cloudflare.com:3478"),
        ("CEHO_TURN_TTL_SECONDS", "0"),
        ("CEHO_TURN_TTL_SECONDS", "soon"),
    ],
)
def test_turn_settings_fail_clearly(name: str, value: str) -> None:
    environment = _environment()
    environment[name] = value
    with pytest.raises(ConfigurationError, match=name):
        HotServerSettings.from_environment(environment)
