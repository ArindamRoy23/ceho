"""Session lifecycle and final-transcript persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Table, create_engine, func, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import KeyedColumnElement

from ceho.hot.config import ConfigurationError
from ceho.hot.outbox import (
    TRANSCRIPT_CREATED,
    DatabaseOutboxStore,
    EventPublisher,
    insert_outbox_event,
    publish_pending,
    transcript_created_payload,
)
from ceho.models import Base, MediaObject, OutboxEvent, Session, SessionSpeaker, Transcript


class TranscriptFinalizeError(Exception):
    """A final transcript was rejected and nothing was committed."""


class InvalidTranscript(TranscriptFinalizeError):
    """The candidate itself cannot be stored."""


class SessionNotFound(TranscriptFinalizeError):
    """No session exists for this tenant and session id."""


class SessionNotActive(TranscriptFinalizeError):
    """The session is not accepting final transcripts."""


class SessionSpeakerNotFound(TranscriptFinalizeError):
    """The session speaker is not a member of this session."""


class MediaNotFound(TranscriptFinalizeError):
    """The media object is not in this session."""


@dataclass(frozen=True)
class FinalizeTranscript:
    """One final direct-source turn to commit."""

    tenant_id: int
    session_id: int
    text: str
    start_ms: int
    end_ms: int
    session_speaker_id: int | None = None
    label: str | None = None
    media_object_id: int | None = None
    media_start_offset_ms: int | None = None
    media_end_offset_ms: int | None = None


@dataclass(frozen=True)
class FinalizedTranscript:
    """Identifiers committed for one final turn."""

    tenant_id: int
    session_id: int
    turn_index: int
    session_speaker_id: int
    event_id: int
    sequence: int
    start_at: datetime
    end_at: datetime


def create_database_engine(database_url: str) -> Engine:
    """Open a PostgreSQL engine. The connection is not made until first use."""
    return create_engine(_postgres_driver_url(database_url), pool_pre_ping=True)


def finalize_transcript(
    connection: Connection,
    command: FinalizeTranscript,
    publisher: EventPublisher | None = None,
) -> FinalizedTranscript:
    """Commit one final transcript and its ``transcript.created`` event.

    The session row is locked for the transaction. ``turn_index`` and the
    outbox ``sequence`` are allocated in SQL. When ``publisher`` is given, pending
    events are pushed after the commit. A bus failure leaves the row pending.
    """
    _validate(command)
    with connection.begin():
        finalized = _commit(connection, command)
    if publisher is not None:
        publish_pending(
            DatabaseOutboxStore(
                connection,
                tenant_id=finalized.tenant_id,
                session_id=finalized.session_id,
            ),
            publisher,
        )
    return finalized


def _postgres_driver_url(database_url: str) -> str:
    parsed = urlparse(database_url)
    if parsed.scheme in {"postgresql", "postgres"}:
        return database_url.replace(f"{parsed.scheme}://", "postgresql+psycopg://", 1)
    if parsed.scheme == "postgresql+psycopg":
        return database_url
    raise ConfigurationError("DATABASE_URL must be a postgresql URI")


def _validate(command: FinalizeTranscript) -> None:
    if not command.text or command.text != command.text.strip():
        raise InvalidTranscript("transcript text must be non-empty and stripped")
    if command.end_ms < command.start_ms:
        raise InvalidTranscript("end_ms must not precede start_ms")
    media = (
        command.media_object_id,
        command.media_start_offset_ms,
        command.media_end_offset_ms,
    )
    if any(value is not None for value in media) and any(value is None for value in media):
        raise InvalidTranscript(
            "media provenance requires media_object_id, media_start_offset_ms, "
            "and media_end_offset_ms together"
        )
    if (
        command.media_start_offset_ms is not None
        and command.media_end_offset_ms is not None
        and (
            command.media_start_offset_ms < 0
            or command.media_end_offset_ms < command.media_start_offset_ms
        )
    ):
        raise InvalidTranscript("media offsets must be a non-negative interval")
    if command.label is not None and (not command.label or command.label != command.label.strip()):
        raise InvalidTranscript("session speaker label must be non-blank")


def _table(model: type[Base]) -> Table:
    relation = model.__table__
    if not isinstance(relation, Table):
        raise TypeError(model.__name__)
    return relation


def _commit(connection: Connection, command: FinalizeTranscript) -> FinalizedTranscript:
    sessions = _table(Session)
    session = (
        connection.execute(
            select(sessions.c.status, sessions.c.started_at)
            .where(
                sessions.c.tenant_id == command.tenant_id,
                sessions.c.session_id == command.session_id,
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if session is None:
        raise SessionNotFound(
            f"session {command.session_id} was not found for tenant {command.tenant_id}"
        )
    if session["status"] != "active":
        raise SessionNotActive(f"session {command.session_id} is not active")

    session_speaker_id = _session_speaker(connection, command)
    _require_media(connection, command)
    turn_index = _next_number(connection, _table(Transcript).c.turn_index, command)
    start_at = session["started_at"] + timedelta(milliseconds=command.start_ms)
    end_at = session["started_at"] + timedelta(milliseconds=command.end_ms)
    transcripts = _table(Transcript)
    connection.execute(
        insert(transcripts).values(
            tenant_id=command.tenant_id,
            session_id=command.session_id,
            turn_index=turn_index,
            session_speaker_id=session_speaker_id,
            start_at=start_at,
            end_at=end_at,
            text=command.text,
            media_object_id=command.media_object_id,
            media_start_offset_ms=command.media_start_offset_ms,
            media_end_offset_ms=command.media_end_offset_ms,
        )
    )
    sequence = _next_number(connection, _table(OutboxEvent).c.sequence, command)
    event_id = insert_outbox_event(
        connection,
        tenant_id=command.tenant_id,
        session_id=command.session_id,
        event_type=TRANSCRIPT_CREATED,
        sequence=sequence,
        payload=transcript_created_payload(
            turn_index=turn_index,
            session_speaker_id=session_speaker_id,
            text=command.text,
            start_at=start_at,
            end_at=end_at,
            media_object_id=command.media_object_id,
            media_start_offset_ms=command.media_start_offset_ms,
            media_end_offset_ms=command.media_end_offset_ms,
        ),
    )
    return FinalizedTranscript(
        tenant_id=command.tenant_id,
        session_id=command.session_id,
        turn_index=turn_index,
        session_speaker_id=session_speaker_id,
        event_id=event_id,
        sequence=sequence,
        start_at=start_at,
        end_at=end_at,
    )


def _session_speaker(connection: Connection, command: FinalizeTranscript) -> int:
    speakers = _table(SessionSpeaker)
    if command.session_speaker_id is None:
        try:
            return int(
                connection.execute(
                    insert(speakers)
                    .values(
                        tenant_id=command.tenant_id,
                        session_id=command.session_id,
                        label=command.label,
                    )
                    .returning(speakers.c.session_speaker_id)
                ).scalar_one()
            )
        except IntegrityError as error:
            if "session_speakers_label_unique" not in str(error):
                raise
            raise InvalidTranscript(
                f"session speaker label {command.label!r} is already used in this session"
            ) from error
    found = connection.execute(
        select(speakers.c.session_speaker_id).where(
            speakers.c.tenant_id == command.tenant_id,
            speakers.c.session_id == command.session_id,
            speakers.c.session_speaker_id == command.session_speaker_id,
        )
    ).scalar_one_or_none()
    if found is None:
        raise SessionSpeakerNotFound(
            f"session speaker {command.session_speaker_id} was not found "
            f"in session {command.session_id}"
        )
    return int(found)


def _require_media(connection: Connection, command: FinalizeTranscript) -> None:
    if command.media_object_id is None:
        return
    media = _table(MediaObject)
    found = connection.execute(
        select(media.c.media_object_id).where(
            media.c.tenant_id == command.tenant_id,
            media.c.session_id == command.session_id,
            media.c.media_object_id == command.media_object_id,
        )
    ).scalar_one_or_none()
    if found is None:
        raise MediaNotFound(
            f"media object {command.media_object_id} was not found in session {command.session_id}"
        )


def _next_number(
    connection: Connection,
    column: KeyedColumnElement[Any],
    command: FinalizeTranscript,
) -> int:
    table = column.table
    if table is None:
        raise TypeError("counter column has no table")
    allocated = connection.execute(
        select(func.coalesce(func.max(column), -1) + 1).where(
            table.c.tenant_id == command.tenant_id,
            table.c.session_id == command.session_id,
        )
    ).scalar_one()
    return int(allocated)
