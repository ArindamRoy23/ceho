"""Tests for hot-server startup configuration."""

import os
import unittest
from unittest.mock import patch

from ceho.hot.config import ConfigurationError, HotServerSettings


class HotServerSettingsTest(unittest.TestCase):
    """Verify required hot-server startup settings."""

    def test_missing_required_setting_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "CEHO_SIGNALING_SECRET"):
            HotServerSettings.from_environment(
                {
                    "DATABASE_URL": "postgresql://ceho:ceho@localhost/ceho",
                    "CEHO_MEDIA_STORAGE_URI": "file:///tmp/ceho-media",
                    "CEHO_EVENT_PUBLISHER_URL": "memory://ceho",
                }
            )

    def test_valid_environment_builds_settings(self) -> None:
        settings = HotServerSettings.from_environment(
            {
                "DATABASE_URL": "postgresql://ceho:ceho@localhost/ceho",
                "CEHO_MEDIA_STORAGE_URI": "file:///tmp/ceho-media",
                "CEHO_SIGNALING_SECRET": "test-secret",
                "CEHO_EVENT_PUBLISHER_URL": "memory://ceho",
                "CEHO_HOST": "0.0.0.0",
                "CEHO_PORT": "9000",
            }
        )

        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9000)

    def test_explicit_mapping_does_not_load_dotenv(self) -> None:
        with patch("ceho.hot.config.load_dotenv") as load_dotenv:
            with self.assertRaisesRegex(ConfigurationError, "CEHO_SIGNALING_SECRET"):
                HotServerSettings.from_environment(
                    {
                        "DATABASE_URL": "postgresql://ceho:ceho@localhost/ceho",
                        "CEHO_MEDIA_STORAGE_URI": "file:///tmp/ceho-media",
                        "CEHO_EVENT_PUBLISHER_URL": "memory://ceho",
                    }
                )

        load_dotenv.assert_not_called()

    def test_process_environment_loads_dotenv(self) -> None:
        values = {
            "DATABASE_URL": "postgresql://ceho:ceho@localhost/ceho",
            "CEHO_MEDIA_STORAGE_URI": "file:///tmp/ceho-media",
            "CEHO_SIGNALING_SECRET": "from-dotenv",
            "CEHO_EVENT_PUBLISHER_URL": "memory://ceho",
        }

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
