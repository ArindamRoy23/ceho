"""Tests for the hot-server ASGI application boundary."""

import unittest

from fastapi.testclient import TestClient

from ceho.hot.config import HotServerSettings
from ceho.hot.server import create_app


class HotServerApplicationTest(unittest.TestCase):
    """Verify the health endpoint without starting any hot-path services."""

    def test_health_endpoint_reports_ready(self) -> None:
        settings = HotServerSettings(
            database_url="postgresql://ceho:ceho@localhost/ceho",
            media_storage_uri="file:///tmp/ceho-media",
            signaling_secret="test-secret",
            event_publisher_url="memory://ceho",
            host="127.0.0.1",
            port=8000,
        )

        with TestClient(create_app(settings)) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
