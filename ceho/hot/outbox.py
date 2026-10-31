"""Transactional-outbox publication and retry boundary."""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

from sqlalchemy import Table, and_, exists, func, insert, or_, select, update
from sqlalchemy.engine import Connection

from ceho.hot.config import ConfigurationError
from ceho.models import Base, OutboxEvent

logger = logging.getLogger(__name__)

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


class MemoryEventPublisher:
    """In-process bus for ``memory://``. Envelopes stay in call order."""

    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    @property
    def preserves_session_order(self) -> bool:
        return True

    def publish(self, envelope: EventEnvelope) -> None:
        self.events.append(envelope)


def publisher_from_url(url: str) -> EventPublisher:
    """Build the publisher selected by ``CEHO_EVENT_PUBLISHER_URL``."""
    scheme = urlparse(url).scheme
    if scheme == "memory":
        return MemoryEventPublisher()
    raise ConfigurationError(
        f"CEHO_EVENT_PUBLISHER_URL scheme {scheme!r} is not supported; use memory://"
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
            try:
                publisher.publish(envelope)
                store.mark_published(envelope.event_id)
            except Exception:
                logger.exception(
                    "outbox event %s for session %s was not published",
                    envelope.event_id,
                    envelope.session_id,
                )
                blocked.add((envelope.tenant_id, envelope.session_id))
                continue
            logger.info(
                "published outbox event %s type %s session %s sequence %s",
                envelope.event_id,
                envelope.event_type,
                envelope.session_id,
                envelope.sequence,
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
                raise OutboxPublishError(f"outbox event {event_id} was not pending")
