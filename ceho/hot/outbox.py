"""Transactional-outbox publication and retry boundary."""

from __future__ import annotations

import json
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

from sqlalchemy import Table, and_, exists, func, insert, or_, select, text, update
from sqlalchemy.engine import Connection, Engine

from ceho.hot.config import ConfigurationError
from ceho.logging import get_logger
from ceho.models import Base, OutboxEvent

logger = get_logger(__name__)

SCHEMA_VERSION = 1
TRANSCRIPT_CREATED = "transcript.created"
OUTBOX_POLL_INTERVAL_SECONDS = 1.0

_publish_lock = threading.Lock()


class OutboxPublishError(RuntimeError):
    """A claimed outbox event could not be marked published."""


@dataclass(frozen=True)
class EventEnvelope:
    """Versioned event handed to a publisher. Consumers dedupe on ``event_id``."""

    event_id: int
    schema_version: int
    event_type: str
    tenant_id: int
    session_id: int
    sequence: int
    occurred_at: datetime
    payload: Mapping[str, object]


class EventPublisher(Protocol):
    """Bus that accepts one envelope per call."""

    @property
    def preserves_session_order(self) -> bool:
        """Whether this bus keeps one session's sends in call order."""

    def publish(self, envelope: EventEnvelope) -> None:
        """Hand one envelope to the bus. Raise when the bus does not accept it."""


class OutboxStore(Protocol):
    """Pending-event claim surface used by ``publish_pending``."""

    def claim_next(self, blocked_sessions: Collection[tuple[int, int]]) -> EventEnvelope | None:
        """Commit an attempt for the next publishable row, leaving it pending."""

    def mark_published(self, event_id: int) -> None:
        """Record that ``event_id`` was accepted by the bus."""


class EventQueueError(RuntimeError):
    """A queue message could not be read or archived."""


@dataclass(frozen=True)
class QueuedMessage:
    """One claimed queue message and the envelope it carries."""

    message_id: int
    read_count: int
    envelope: EventEnvelope


class PgmqPublisher:
    """pgmq queue selected by ``pgmq://<name>``. See https://github.com/pgmq/pgmq.

    ``publish`` calls ``pgmq.send``. ``read`` calls ``pgmq.read`` and hides each
    message for the visibility timeout. ``archive`` calls ``pgmq.archive``.
    Another ``CEHO_EVENT_PUBLISHER_URL`` scheme implements ``EventPublisher``
    and does not use this queue.
    """

    def __init__(self, engine: Engine, queue_name: str) -> None:
        self._engine = engine
        self._queue_name = _queue_name(queue_name)
        self._created = False

    @property
    def queue_name(self) -> str:
        return self._queue_name

    @property
    def preserves_session_order(self) -> bool:
        return True

    def publish(self, envelope: EventEnvelope) -> None:
        with self._engine.begin() as connection:
            self._ensure_queue(connection)
            connection.execute(
                text("SELECT pgmq.send(CAST(:queue_name AS text), CAST(:message AS jsonb))"),
                {"queue_name": self._queue_name, "message": json.dumps(_message_body(envelope))},
            )
        self._created = True

    def read(self, *, batch: int = 1, visibility_seconds: int = 30) -> list[QueuedMessage]:
        """Claim up to ``batch`` visible messages, hiding each for ``visibility_seconds``."""
        if batch < 1:
            raise EventQueueError("batch must be positive")
        if visibility_seconds < 0:
            raise EventQueueError("visibility_seconds must be non-negative")
        with self._engine.begin() as connection:
            self._ensure_queue(connection)
            rows = (
                connection.execute(
                    text(
                        "SELECT msg_id, read_ct, message "
                        "FROM pgmq.read(CAST(:queue_name AS text), :vt, :qty)"
                    ),
                    {
                        "queue_name": self._queue_name,
                        "vt": visibility_seconds,
                        "qty": batch,
                    },
                )
                .mappings()
                .all()
            )
        self._created = True
        return [
            QueuedMessage(
                message_id=int(row["msg_id"]),
                read_count=int(row["read_ct"]),
                envelope=_envelope_from_message(row["message"]),
            )
            for row in sorted(rows, key=lambda row: int(row["msg_id"]))
        ]

    def archive(self, message_id: int) -> None:
        """Move one message on this queue into the pgmq archive."""
        with self._engine.begin() as connection:
            self._ensure_queue(connection)
            archived = connection.execute(
                text("SELECT pgmq.archive(CAST(:queue_name AS text), :msg_id)"),
                {"queue_name": self._queue_name, "msg_id": message_id},
            ).scalar_one()
        self._created = True
        if archived is not True:
            raise EventQueueError(f"queue message {message_id} was not available")

    def _ensure_queue(self, connection: Connection) -> None:
        if self._created:
            return
        connection.execute(
            text("SELECT pgmq.create(CAST(:queue_name AS text))"),
            {"queue_name": self._queue_name},
        )


def publisher_from_url(url: str, engine: Engine) -> EventPublisher:
    """Build the publisher selected by ``CEHO_EVENT_PUBLISHER_URL``.

    ``pgmq://<name>`` sends through the pgmq extension. A different bus is a
    new scheme in this function that still accepts ``EventEnvelope``.
    """
    parsed = urlparse(url)
    if parsed.scheme == "pgmq":
        return PgmqPublisher(engine, parsed.netloc or parsed.path)
    raise ConfigurationError(
        f"CEHO_EVENT_PUBLISHER_URL scheme {parsed.scheme!r} is not supported; use pgmq://"
    )


def _queue_name(raw: str) -> str:
    name = raw.strip().strip("/")
    if (
        not name
        or name != name.strip()
        or len(name) > 47
        or "/" in name
        or any(token in name for token in ("$", ";", "--", "'"))
    ):
        raise ConfigurationError("CEHO_EVENT_PUBLISHER_URL must be pgmq://<name>")
    return name


def _message_body(envelope: EventEnvelope) -> dict[str, object]:
    if envelope.occurred_at.tzinfo is None:
        raise EventQueueError("occurred_at must include a timezone")
    return {
        "event_id": envelope.event_id,
        "schema_version": envelope.schema_version,
        "event_type": envelope.event_type,
        "tenant_id": envelope.tenant_id,
        "session_id": envelope.session_id,
        "sequence": envelope.sequence,
        "occurred_at": envelope.occurred_at.isoformat(),
        "payload": dict(envelope.payload),
    }


def _envelope_from_message(body: object) -> EventEnvelope:
    if not isinstance(body, dict):
        raise EventQueueError("queue message is not an object")
    payload = body.get("payload")
    occurred_at = body.get("occurred_at")
    if not isinstance(payload, dict) or not isinstance(occurred_at, str):
        raise EventQueueError("queue message is missing its envelope")
    return EventEnvelope(
        event_id=int(body["event_id"]),
        schema_version=int(body["schema_version"]),
        event_type=str(body["event_type"]),
        tenant_id=int(body["tenant_id"]),
        session_id=int(body["session_id"]),
        sequence=int(body["sequence"]),
        occurred_at=datetime.fromisoformat(occurred_at),
        payload=payload,
    )


def transcript_created_payload(
    *,
    turn_index: int,
    session_speaker_id: int,
    text: str,
    start_at: datetime,
    end_at: datetime,
    media_object_id: int | None,
    media_start_offset_ms: int | None,
    media_end_offset_ms: int | None,
) -> dict[str, object]:
    """JSON body stored on a ``transcript.created`` outbox row."""
    return {
        "turn_index": turn_index,
        "session_speaker_id": session_speaker_id,
        "text": text,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "media_object_id": media_object_id,
        "media_start_offset_ms": media_start_offset_ms,
        "media_end_offset_ms": media_end_offset_ms,
    }


def _table(model: type[Base]) -> Table:
    relation = model.__table__
    if not isinstance(relation, Table):
        raise TypeError(model.__name__)
    return relation


def insert_outbox_event(
    connection: Connection,
    *,
    tenant_id: int,
    session_id: int,
    event_type: str,
    sequence: int,
    payload: Mapping[str, object],
) -> int:
    """Insert one pending outbox row. The caller owns the transaction."""
    outbox = _table(OutboxEvent)
    event_id = connection.execute(
        insert(outbox)
        .values(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type,
            schema_version=SCHEMA_VERSION,
            sequence=sequence,
            payload=dict(payload),
        )
        .returning(outbox.c.event_id)
    ).scalar_one()
    return int(event_id)


def publish_pending(store: OutboxStore, publisher: EventPublisher) -> int:
    """Send pending events. Return how many were marked published.

    A send or completion failure leaves that session's head row pending and
    does not publish a later sequence for the session on this pass. Other
    sessions continue. The same ``event_id`` is sent again on a later pass.
    """
    with _publish_lock:
        published = 0
        blocked: set[tuple[int, int]] = set()
        while True:
            envelope = store.claim_next(blocked)
            if envelope is None:
                return published
            logger.debug(
                "outbox event claimed",
                outbox_id=envelope.event_id,
                tenant_id=envelope.tenant_id,
                session_id=envelope.session_id,
                sequence=envelope.sequence,
            )
            try:
                publisher.publish(envelope)
                store.mark_published(envelope.event_id)
            except Exception:
                logger.warning(
                    "outbox event not published",
                    outbox_id=envelope.event_id,
                    tenant_id=envelope.tenant_id,
                    session_id=envelope.session_id,
                    exc_info=True,
                )
                blocked.add((envelope.tenant_id, envelope.session_id))
                continue
            logger.info(
                "outbox event published",
                outbox_id=envelope.event_id,
                tenant_id=envelope.tenant_id,
                session_id=envelope.session_id,
                sequence=envelope.sequence,
                event_type=envelope.event_type,
            )
            published += 1


class DatabaseOutboxStore:
    """Claim and complete ``outbox_events`` rows through one connection.

    ``tenant_id`` and ``session_id`` together limit the claim to one session.
    The process drain leaves both unset and publishes every pending row.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        tenant_id: int | None = None,
        session_id: int | None = None,
    ) -> None:
        if (tenant_id is None) != (session_id is None):
            raise OutboxPublishError("tenant_id and session_id are scoped together")
        self._connection = connection
        self._tenant_id = tenant_id
        self._session_id = session_id

    def claim_next(self, blocked_sessions: Collection[tuple[int, int]]) -> EventEnvelope | None:
        outbox = _table(OutboxEvent)
        earlier = outbox.alias("earlier")
        conditions = [
            outbox.c.publication_status == "pending",
            ~exists(
                select(earlier.c.event_id).where(
                    earlier.c.tenant_id == outbox.c.tenant_id,
                    earlier.c.session_id == outbox.c.session_id,
                    earlier.c.sequence < outbox.c.sequence,
                    earlier.c.publication_status != "published",
                )
            ),
        ]
        if self._tenant_id is not None and self._session_id is not None:
            conditions.append(outbox.c.tenant_id == self._tenant_id)
            conditions.append(outbox.c.session_id == self._session_id)
        if blocked_sessions:
            conditions.append(
                and_(
                    *[
                        or_(
                            outbox.c.tenant_id != tenant_id,
                            outbox.c.session_id != session_id,
                        )
                        for tenant_id, session_id in blocked_sessions
                    ]
                )
            )
        statement = (
            select(
                outbox.c.event_id,
                outbox.c.tenant_id,
                outbox.c.session_id,
                outbox.c.event_type,
                outbox.c.schema_version,
                outbox.c.sequence,
                outbox.c.payload,
                outbox.c.created_at,
            )
            .where(*conditions)
            .order_by(outbox.c.tenant_id, outbox.c.session_id, outbox.c.sequence)
            .limit(1)
            .with_for_update(skip_locked=True, of=outbox.c.event_id)
        )
        with self._connection.begin():
            row = self._connection.execute(statement).mappings().first()
            if row is None:
                return None
            self._connection.execute(
                update(outbox)
                .where(outbox.c.event_id == row["event_id"])
                .values(
                    attempt_count=outbox.c.attempt_count + 1,
                    last_attempt_at=func.now(),
                )
            )
            payload = row["payload"]
            if not isinstance(payload, dict):
                logger.error(
                    "outbox event payload invalid",
                    outbox_id=int(row["event_id"]),
                    tenant_id=int(row["tenant_id"]),
                    session_id=int(row["session_id"]),
                    error="payload is not an object",
                )
                raise OutboxPublishError(f"outbox event {row['event_id']} payload is not an object")
            return EventEnvelope(
                event_id=int(row["event_id"]),
                schema_version=int(row["schema_version"]),
                event_type=str(row["event_type"]),
                tenant_id=int(row["tenant_id"]),
                session_id=int(row["session_id"]),
                sequence=int(row["sequence"]),
                occurred_at=row["created_at"],
                payload=payload,
            )

    def mark_published(self, event_id: int) -> None:
        outbox = _table(OutboxEvent)
        with self._connection.begin():
            result = self._connection.execute(
                update(outbox)
                .where(
                    outbox.c.event_id == event_id,
                    outbox.c.publication_status == "pending",
                )
                .values(publication_status="published", published_at=func.now())
            )
            if result.rowcount != 1:
                logger.error(
                    "outbox event not pending",
                    outbox_id=event_id,
                    error="outbox event was not pending",
                )
                raise OutboxPublishError(f"outbox event {event_id} was not pending")
