"""Tests for the hot-server ASGI application boundary."""

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ceho.hot.config import ConfigurationError, HotServerSettings
from ceho.hot.server import create_app, main
from ceho.logging import LoggingConfigurationError


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
            event_publisher_url="pgmq://ceho",
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
            event_publisher_url="pgmq://ceho",
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
        self.assertIn("log_config", run.call_args.kwargs)
        self.assertIsNone(run.call_args.kwargs["log_config"])
        app = run.call_args.args[0]
        self.assertIsNotNone(app.state.engine)
        self.assertIsNotNone(app.state.publisher)


class HotServerOutboxDrainTest(unittest.TestCase):
    """The process drains pending outbox rows when a database engine is configured."""

    def test_engine_drains_the_outbox_without_blocking_health(self) -> None:
        started = threading.Event()

        def publish(engine: object, publisher: object) -> None:
            started.set()

        settings = HotServerSettings(
            database_url="postgresql://ceho:ceho@localhost/ceho",
            media_storage_uri="s3://ceho-media",
            s3_endpoint_url="http://minio:9000",
            s3_access_key_id="ceho",
            s3_secret_access_key="ceho-dev-only",
            s3_region="us-east-1",
            signaling_secret="test-secret",
            event_publisher_url="pgmq://ceho",
            host="127.0.0.1",
            port=8000,
        )
        engine = create_engine("postgresql+psycopg://ceho:ceho@localhost/ceho")

        with (
            patch("ceho.hot.server._publish_with_engine", publish),
            TestClient(create_app(settings, engine=engine)) as client,
        ):
            self.assertTrue(started.wait(2))
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


def _settings() -> HotServerSettings:
    return HotServerSettings(
        database_url="postgresql://ceho:ceho@localhost/ceho",
        media_storage_uri="s3://ceho-media",
        s3_endpoint_url="http://minio:9000",
        s3_access_key_id="ceho",
        s3_secret_access_key="ceho-dev-only",
        s3_region="us-east-1",
        signaling_secret="test-secret",
        event_publisher_url="pgmq://ceho",
        host="127.0.0.1",
        port=8000,
    )


def _fields(record: object) -> dict[str, object]:
    fields = getattr(record, "ceho_fields", None)
    assert isinstance(fields, dict)
    return fields


def test_lifespan_logs_start_and_stop(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="ceho.hot.server")

    with TestClient(create_app(_settings())) as client:
        response = client.get("/health")

    assert response.status_code == 200
    records = [record for record in caplog.records if record.name == "ceho.hot.server"]
    assert [record.msg for record in records] == ["hot server started", "hot server stopped"]
    assert _fields(records[0]) == {"host": "127.0.0.1", "port": 8000}
    assert _fields(records[1]) == {"host": "127.0.0.1", "port": 8000}


def test_outbox_drain_failure_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="ceho.hot.server")
    slept = threading.Event()

    def publish(engine: object, publisher: object) -> int:
        raise RuntimeError("db down")

    async def block_until_cancelled(_seconds: float) -> None:
        slept.set()
        await asyncio.Future()

    engine = create_engine("postgresql+psycopg://ceho:ceho@localhost/ceho")
    with (
        patch("ceho.hot.server._publish_with_engine", publish),
        patch("ceho.hot.server.asyncio.sleep", block_until_cancelled),
        TestClient(create_app(_settings(), engine=engine)),
    ):
        assert slept.wait(2)

    records = [record for record in caplog.records if record.msg == "outbox drain failed"]
    assert records
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], RuntimeError)


def test_outbox_drain_logs_how_many_rows_were_published(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="ceho.hot.server")
    slept = threading.Event()

    def publish(engine: object, publisher: object) -> int:
        return 4

    async def block_until_cancelled(_seconds: float) -> None:
        slept.set()
        await asyncio.Future()

    engine = create_engine("postgresql+psycopg://ceho:ceho@localhost/ceho")
    with (
        patch("ceho.hot.server._publish_with_engine", publish),
        patch("ceho.hot.server.asyncio.sleep", block_until_cancelled),
        TestClient(create_app(_settings(), engine=engine)),
    ):
        assert slept.wait(2)

    records = [record for record in caplog.records if record.msg == "outbox drain finished"]
    assert records
    assert records[0].levelname == "DEBUG"
    assert _fields(records[0])["published"] == 4


def test_main_rejects_an_unknown_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEHO_LOG_LEVEL", "verbose")

    with (
        patch("ceho.hot.server.uvicorn.run") as run,
        pytest.raises(LoggingConfigurationError, match="^CEHO_LOG_LEVEL must be "),
    ):
        main()

    run.assert_not_called()


def test_rejected_configuration_logs_critical_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(
            "ceho.hot.server.HotServerSettings.from_environment",
            side_effect=ConfigurationError("DATABASE_URL must be configured"),
        ),
        patch("ceho.hot.server.uvicorn.run") as run,
        pytest.raises(ConfigurationError, match="DATABASE_URL"),
    ):
        main()

    run.assert_not_called()
    line = _stderr_line(capsys.readouterr().err, "hot server exiting")
    assert line["level"] == "CRITICAL"
    assert line["error"] == "DATABASE_URL must be configured"


def test_listen_socket_failure_logs_critical_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(
            "ceho.hot.server.HotServerSettings.from_environment",
            return_value=_settings(),
        ),
        patch("ceho.hot.server.uvicorn.run", side_effect=OSError("address already in use")),
        pytest.raises(OSError, match="address already in use"),
    ):
        main()

    line = _stderr_line(capsys.readouterr().err, "hot server exiting")
    assert line["level"] == "CRITICAL"
    assert line["error"] == "address already in use"


def _stderr_line(err: str, msg: str) -> dict[str, object]:
    matches = [json.loads(line) for line in err.splitlines() if line.strip() and msg in line]
    assert matches, err
    last = matches[-1]
    assert isinstance(last, dict)
    return last
