"""Process logging: one level, one stderr handler, one JSON line."""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from types import TracebackType

from dotenv import load_dotenv
from loguru import logger as loguru_logger

_INVALID_LEVEL = "CEHO_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_RESERVED_FIELDS = frozenset({"ts", "level", "logger", "msg", "exc"})
_OWNERS = ("ceho", "uvicorn", "sqlalchemy", "pipecat")
_CHILDREN = (
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
)

_handler: logging.Handler | None = None
_loguru_sink_id: int | None = None


class LoggingConfigurationError(ValueError):
    """Raised when ``CEHO_LOG_LEVEL`` is not a standard level."""


class CehoLogger:
    """Standard-library logger whose keyword arguments become JSON fields."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, msg: str, **fields: object) -> None:
        self._emit(logging.DEBUG, msg, fields)

    def info(self, msg: str, **fields: object) -> None:
        self._emit(logging.INFO, msg, fields)

    def warning(self, msg: str, **fields: object) -> None:
        self._emit(logging.WARNING, msg, fields)

    def error(self, msg: str, **fields: object) -> None:
        self._emit(logging.ERROR, msg, fields)

    def critical(self, msg: str, **fields: object) -> None:
        self._emit(logging.CRITICAL, msg, fields)

    def exception(self, msg: str, **fields: object) -> None:
        self._emit(logging.ERROR, msg, fields, exc_info=True)

    def _emit(
        self,
        level: int,
        msg: str,
        fields: Mapping[str, object],
        *,
        exc_info: bool = False,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return
        extra = dict(fields)
        requested = extra.pop("exc_info", None)
        self._logger.log(
            level,
            msg,
            extra={"ceho_fields": extra},
            exc_info=exc_info or requested is True,
        )


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "ceho_fields", None)
        return _json_line(
            record.created,
            record.levelname,
            record.name,
            record.getMessage(),
            fields if isinstance(fields, dict) else None,
            record.exc_info,
        )


def get_logger(name: str) -> CehoLogger:
    """Return the process logger for ``name``. Bind it once at module scope."""
    return CehoLogger(logging.getLogger(name))


def configure(environment: Mapping[str, str] | None = None) -> None:
    """Set the process level and write every log as one JSON line on stderr.

    With no mapping, load ``.env`` and then read ``CEHO_LOG_LEVEL`` from the
    process environment. Names already set in the process stay in place. An
    explicit mapping is used as given and does not load ``.env``.
    """
    global _handler, _loguru_sink_id
    level_name = _level_name(environment)
    level = _LEVELS[level_name]
    if _handler is None:
        _handler = logging.StreamHandler(sys.stderr)
        _handler.setFormatter(_JsonFormatter())
    _handler.setLevel(level)
    for name in _OWNERS:
        lib = logging.getLogger(name)
        lib.setLevel(level)
        lib.propagate = False
        if _handler not in lib.handlers:
            lib.addHandler(_handler)
    for name in _CHILDREN:
        lib = logging.getLogger(name)
        lib.setLevel(level)
        lib.propagate = True
        if _handler in lib.handlers:
            lib.removeHandler(_handler)
    if _loguru_sink_id is None:
        loguru_logger.remove()
    else:
        loguru_logger.remove(_loguru_sink_id)
    _loguru_sink_id = int(
        loguru_logger.add(
            _loguru_sink,
            level=level_name,
            format="{message}",
            colorize=False,
            enqueue=False,
            backtrace=False,
            diagnose=False,
        )
    )


def reset_logging() -> None:
    """Remove the handler and loguru sink installed by :func:`configure`."""
    global _handler, _loguru_sink_id
    if _loguru_sink_id is not None:
        loguru_logger.remove(_loguru_sink_id)
        _loguru_sink_id = None
        loguru_logger.add(sys.stderr)
    handler = _handler
    _handler = None
    if handler is None:
        return
    for name in (*_OWNERS, *_CHILDREN):
        lib = logging.getLogger(name)
        lib.removeHandler(handler)
        lib.setLevel(logging.NOTSET)
        lib.propagate = True


def _level_name(environment: Mapping[str, str] | None) -> str:
    if environment is None:
        load_dotenv()
        source: Mapping[str, str] = os.environ
    else:
        source = environment
    name = source.get("CEHO_LOG_LEVEL", "INFO").strip()
    if name not in _LEVELS:
        raise LoggingConfigurationError(_INVALID_LEVEL)
    return name


def _timestamp(created: float) -> str:
    moment = datetime.fromtimestamp(created, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _exc_payload(
    exc_info: tuple[type[BaseException] | None, BaseException | None, TracebackType | None]
    | tuple[None, None, None]
    | None,
) -> dict[str, str] | None:
    if not exc_info:
        return None
    exc_type, exc_value, exc_tb = exc_info
    if exc_type is None:
        return None
    return {
        "type": exc_type.__name__,
        "msg": "" if exc_value is None else str(exc_value),
        "trace": "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    }


def _json_line(
    created: float,
    level: str,
    logger_name: str,
    msg: str,
    fields: Mapping[str, object] | None,
    exc_info: tuple[type[BaseException] | None, BaseException | None, TracebackType | None]
    | tuple[None, None, None]
    | None,
) -> str:
    payload: dict[str, object] = {
        "ts": _timestamp(created),
        "level": level,
        "logger": logger_name,
        "msg": msg,
    }
    if fields:
        for key in sorted(fields):
            if key not in _RESERVED_FIELDS:
                payload[key] = fields[key]
    exc = _exc_payload(exc_info)
    if exc is not None:
        payload["exc"] = exc
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _loguru_sink(message: object) -> None:
    record = getattr(message, "record", None)
    if not isinstance(record, Mapping):
        return
    level = record.get("level")
    level_name = getattr(level, "name", None)
    if level_name not in _LEVELS:
        return
    moment = record.get("time")
    created = (
        moment.timestamp()
        if isinstance(moment, datetime)
        else datetime.now(timezone.utc).timestamp()
    )
    line = _json_line(
        created,
        str(level_name),
        str(record.get("name") or "pipecat"),
        str(message).rstrip("\n"),
        None,
        _loguru_exc(record.get("exception")),
    )
    sys.stderr.write(line + "\n")


def _loguru_exc(
    exception: object,
) -> tuple[type[BaseException], BaseException, TracebackType | None] | None:
    if exception is None:
        return None
    exc_type = getattr(exception, "type", None)
    if not isinstance(exc_type, type) or not issubclass(exc_type, BaseException):
        return None
    value = getattr(exception, "value", None)
    if not isinstance(value, BaseException):
        value = exc_type(str(value))
    traceback_value = getattr(exception, "traceback", None)
    if traceback_value is not None and not isinstance(traceback_value, TracebackType):
        traceback_value = None
    return (exc_type, value, traceback_value)
