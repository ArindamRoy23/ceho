"""Tests for hot-server startup configuration."""

import unittest

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
