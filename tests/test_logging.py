"""Process logging level, JSON line, and library attachment."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping

import pytest
from loguru import logger as loguru_logger

from ceho.logging import LoggingConfigurationError, configure, get_logger

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
_INVALID_LEVEL = "CEHO_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"


def _lines(err: str) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for line in err.splitlines():
        if line.strip():
            parsed.append(json.loads(line))
    return parsed


def _line(err: str, msg: str) -> dict[str, object]:
    matches = [line for line in _lines(err) if line.get("msg") == msg]
    assert matches, err
    return matches[-1]


class _Unrendered:
    def __repr__(self) -> str:
        raise AssertionError("silenced log formatted a field")


def test_default_level_is_info(capsys: pytest.CaptureFixture[str]) -> None:
    configure({})
    get_logger("ceho.demo").debug("hidden detail", detail=_Unrendered())
    get_logger("ceho.demo").info("server started")

    line = _line(capsys.readouterr().err, "server started")
    assert line["level"] == "INFO"
    assert line["logger"] == "ceho.demo"
    assert list(line)[:4] == ["ts", "level", "logger", "msg"]
    assert isinstance(line["ts"], str)
    assert re.fullmatch(_TS, line["ts"])


def test_explicit_mapping_ignores_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CEHO_LOG_LEVEL", "DEBUG")
    configure({})
    get_logger("ceho.demo").debug("hidden detail", detail=_Unrendered())

    assert capsys.readouterr().err == ""


def test_process_environment_sets_the_level(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CEHO_LOG_LEVEL", "DEBUG")
    configure()
    get_logger("ceho.demo").debug("frame accepted", source_id="mic")

    line = _line(capsys.readouterr().err, "frame accepted")
    assert line["level"] == "DEBUG"
    assert line["source_id"] == "mic"


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_configured_level_emits_that_call(
    level: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure({"CEHO_LOG_LEVEL": f" {level} "})
    getattr(get_logger("ceho.demo"), level.lower())("level marker")

    line = _line(capsys.readouterr().err, "level marker")
    assert line["level"] == level


@pytest.mark.parametrize("value", ["", " ", "debug", "TRACE", "verbose"])
def test_unknown_log_level_fails_clearly(value: str) -> None:
    environment: Mapping[str, str] = {"CEHO_LOG_LEVEL": value}

    with pytest.raises(LoggingConfigurationError, match=f"^{_INVALID_LEVEL}$"):
        configure(environment)


def test_json_line_sorts_fields_and_puts_exc_last(capsys: pytest.CaptureFixture[str]) -> None:
    configure({"CEHO_LOG_LEVEL": "INFO"})
    try:
        raise RuntimeError("bus down")
    except RuntimeError:
        get_logger("ceho.hot.outbox").exception(
            "outbox event not published",
            source_id="mic",
            outbox_id=7,
        )

    line = _line(capsys.readouterr().err, "outbox event not published")
    assert list(line) == ["ts", "level", "logger", "msg", "outbox_id", "source_id", "exc"]
    assert line["level"] == "ERROR"
    assert line["logger"] == "ceho.hot.outbox"
    assert line["outbox_id"] == 7
    exc = line["exc"]
    assert isinstance(exc, dict)
    assert exc["type"] == "RuntimeError"
    assert exc["msg"] == "bus down"
    assert "RuntimeError" in str(exc["trace"])


def test_library_loggers_use_the_same_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure({"CEHO_LOG_LEVEL": "INFO"})
    logging.getLogger("uvicorn.error").info("uvicorn ready")
    logging.getLogger("sqlalchemy.engine").info("sql ready")
    logging.getLogger("pipecat.pipeline").info("stdlib pipecat ready")
    loguru_logger.info("pipecat ready")

    err = capsys.readouterr().err
    for msg in ("uvicorn ready", "sql ready", "stdlib pipecat ready", "pipecat ready"):
        line = _line(err, msg)
        assert list(line)[:4] == ["ts", "level", "logger", "msg"]
        assert line["level"] == "INFO"
        assert isinstance(line["ts"], str)
        assert re.fullmatch(_TS, line["ts"])
    assert _line(err, "uvicorn ready")["logger"] == "uvicorn.error"
    assert _line(err, "sql ready")["logger"] == "sqlalchemy.engine"
    assert _line(err, "stdlib pipecat ready")["logger"] == "pipecat.pipeline"
