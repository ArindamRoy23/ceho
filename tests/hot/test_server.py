"""Tests for the hot-server ASGI application boundary."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ceho.hot.config import HotServerSettings
from ceho.hot.server import create_app, main


class HotServerApplicationTest(unittest.TestCase):
    """Verify the health endpoint without starting any hot-path services."""

    def test_health_endpoint_reports_ready(self) -> None:
        settings = HotServerSettings(
            database_url="postgresql://ceho:ceho@localhost/ceho",
            media_storage_uri="s3://ceho-media",
            s3_endpoint_url="http://minio:9000",
            s3_access_key_id="ceho",
            s3_secret_access_key="ceho-dev-only",
            s3_region="us-east-1",
            signaling_secret="test-secret",
            event_publisher_url="memory://ceho",
            host="127.0.0.1",
            port=8000,
        )

        with TestClient(create_app(settings)) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class HotServerMainTest(unittest.TestCase):
    """Verify process startup runs the server from environment settings."""

    def test_main_runs_with_environment_settings(self) -> None:
        settings = HotServerSettings(
            database_url="postgresql://ceho:ceho@localhost/ceho",
            media_storage_uri="s3://ceho-media",
            s3_endpoint_url="http://minio:9000",
            s3_access_key_id="ceho",
            s3_secret_access_key="ceho-dev-only",
            s3_region="us-east-1",
            signaling_secret="test-secret",
            event_publisher_url="memory://ceho",
            host="127.0.0.1",
            port=8000,
        )

        with (
            patch(
                "ceho.hot.server.HotServerSettings.from_environment",
                return_value=settings,
            ) as from_environment,
            patch("ceho.hot.server.uvicorn.run") as run,
        ):
            main()

        from_environment.assert_called_once_with()
        self.assertEqual(run.call_args.kwargs["host"], settings.host)
        self.assertEqual(run.call_args.kwargs["port"], settings.port)
