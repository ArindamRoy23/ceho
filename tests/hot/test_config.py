"""Tests for hot-server startup configuration."""

import os
import unittest
from unittest.mock import patch

from ceho.hot.config import (
    DIRECT_SOURCE_PIPELINE,
    ConfigurationError,
    DirectSourcePipelineConfig,
    HotServerSettings,
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
        "CEHO_EVENT_PUBLISHER_URL": "memory://ceho",
    }
    values.update(overrides)
    return values


class HotServerSettingsTest(unittest.TestCase):
    """Verify required hot-server startup settings."""

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
        self.assertEqual(audio.frame_bytes(20), 640)
        self.assertEqual(DIRECT_SOURCE_PIPELINE, audio)

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
