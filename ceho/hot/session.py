"""Session lifecycle and final-transcript persistence boundary."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, NoReturn, Protocol
from urllib.parse import urlparse

from sqlalchemy import Table, create_engine, func, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import KeyedColumnElement

from ceho.hot.config import ConfigurationError
from ceho.hot.outbox import (
    SESSION_CREATED,
    SESSION_ENDED,
    TRANSCRIPT_CREATED,
    DatabaseOutboxStore,
    EventPublisher,
    insert_outbox_event,
    publish_pending,
    transcript_created_payload,
)
from ceho.hot.signaling import issue_signaling_credential
from ceho.logging import get_logger
from ceho.models import (
    GENERIC_TENANT_ID,
    Base,
    Channel,
    MediaObject,
    OutboxEvent,
    Session,
    SessionSpeaker,
    Tenant,
    Transcript,
)

logger = get_logger(__name__)


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
    try:
        _validate(command)
        with connection.begin():
            finalized = _commit(connection, command)
    except TranscriptFinalizeError as error:
        logger.error(
            "transcript rejected",
            tenant_id=command.tenant_id,
            session_id=command.session_id,
            error=str(error),
        )
        raise
    except Exception:
        logger.exception(
            "transcript commit failed",
            tenant_id=command.tenant_id,
            session_id=command.session_id,
        )
        raise
    logger.info(
        "transcript committed",
        tenant_id=finalized.tenant_id,
        session_id=finalized.session_id,
        outbox_id=finalized.event_id,
        turn_index=finalized.turn_index,
    )
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
    logger.debug(
        "transcript commit started",
        tenant_id=command.tenant_id,
        session_id=command.session_id,
        turn_index=turn_index,
        media_present=command.media_object_id is not None,
    )
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


EndReason = Literal["explicit", "idle"]


class ControlError(Exception):
    """A control request that must not change the session."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SessionView:
    """Durable session fields the control API returns and idles on."""

    tenant_id: int
    session_id: int
    channel_tenant_id: int
    channel_id: int
    status: str
    started_at: datetime
    ended_at: datetime | None
    description: str | None


@dataclass(frozen=True)
class IssuedSession:
    """A session and the signaling credential minted for its connection."""

    session: SessionView
    signaling_credential: str
    expires_at: datetime


class ControlStore(Protocol):
    """Persistence boundary for session control. Tests supply a fake."""

    def tenant_exists(self, tenant_id: int) -> bool:
        """Return whether the tenant row exists."""

    def find_channel(self, channel_id: int) -> tuple[int, int] | None:
        """Return channel tenant and channel id, or none when the row is missing."""

    def add_session(
        self,
        *,
        tenant_id: int,
        channel_tenant_id: int,
        channel_id: int,
        description: str | None,
        started_at: datetime,
    ) -> SessionView:
        """Stage an active session and return it with its new id."""

    def add_outbox(
        self,
        *,
        tenant_id: int,
        session_id: int,
        event_type: str,
        sequence: int,
        payload: dict[str, object],
    ) -> int:
        """Stage one pending outbox row for the current transaction."""

    def get_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Return the session in this tenant, or none."""

    def lock_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Lock the session row in this tenant and return it, or none."""

    def mark_ended(self, tenant_id: int, session_id: int, ended_at: datetime) -> None:
        """Stage status ended and ended_at on the locked session."""

    def next_outbox_sequence(self, tenant_id: int, session_id: int) -> int:
        """Return the next per-session outbox sequence."""

    def commit(self) -> None:
        """Commit the staged session change and outbox row together."""

    def rollback(self) -> None:
        """Drop the staged session change and outbox row."""

    def after_commit(
        self,
        publisher: EventPublisher | None,
        *,
        tenant_id: int,
        session_id: int,
    ) -> None:
        """Publish the committed session event. A bus failure leaves it pending."""

    def list_active(self) -> list[SessionView]:
        """Return active sessions across tenants."""


class PresenceBook:
    """In-process peer counts. Join is not a connected peer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peers: dict[tuple[int, int], int] = {}
        self._quiet_since: dict[tuple[int, int], datetime] = {}

    def attach(self, tenant_id: int, session_id: int) -> None:
        """Record one connected peer for a session."""
        key = (tenant_id, session_id)
        with self._lock:
            self._peers[key] = self._peers.get(key, 0) + 1
            self._quiet_since.pop(key, None)

    def detach(self, tenant_id: int, session_id: int, now: datetime) -> None:
        """Record one peer leaving. The idle clock starts when the last peer leaves."""
        key = (tenant_id, session_id)
        with self._lock:
            count = self._peers.get(key, 0)
            if count > 1:
                self._peers[key] = count - 1
                return
            self._peers.pop(key, None)
            if count == 1:
                self._quiet_since[key] = now

    def peer_count(self, tenant_id: int, session_id: int) -> int:
        """Return how many peers are connected for a session."""
        with self._lock:
            return self._peers.get((tenant_id, session_id), 0)

    def ready_to_end(
        self,
        tenant_id: int,
        session_id: int,
        started_at: datetime,
        now: datetime,
        idle_seconds: int,
        *,
        grant_unseen: bool,
        process_started_at: datetime | None,
    ) -> bool:
        """Return whether an active session with no peers has been idle long enough.

        The first sweep after startup grants one idle interval to sessions that
        already existed, and does not end them on that pass.
        """
        key = (tenant_id, session_id)
        with self._lock:
            if self._peers.get(key, 0) > 0:
                return False
            quiet = self._quiet_since.get(key)
            unseen = process_started_at is None or started_at < process_started_at
            if quiet is None and grant_unseen and unseen:
                self._quiet_since[key] = now
                return False
            since = quiet if quiet is not None else started_at
            return now - since >= timedelta(seconds=idle_seconds)


def create_session(
    store: ControlStore,
    *,
    tenant_id: int,
    user_id: int,
    channel_id: int,
    description: str | None,
    now: datetime,
    secret: str,
    ttl_seconds: int,
    publisher: EventPublisher | None = None,
) -> IssuedSession:
    """Insert an active session and its created event, then mint a signaling credential."""
    logger.debug(
        "session create started",
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
    )
    try:
        _require_tenant(store, "session rejected", tenant_id, user_id, None)
        found = store.find_channel(channel_id)
        if found is None:
            _reject(
                "session rejected",
                "channel does not exist",
                404,
                reason="channel_missing",
                tenant_id=tenant_id,
                user_id=user_id,
            )
        channel_tenant_id, found_channel_id = found
        if channel_tenant_id != tenant_id and channel_tenant_id != GENERIC_TENANT_ID:
            _reject(
                "session rejected",
                "channel is not available to this tenant",
                403,
                reason="channel_forbidden",
                tenant_id=tenant_id,
                user_id=user_id,
            )
        session = store.add_session(
            tenant_id=tenant_id,
            channel_tenant_id=channel_tenant_id,
            channel_id=found_channel_id,
            description=_description(description),
            started_at=now,
        )
        event_id = store.add_outbox(
            tenant_id=tenant_id,
            session_id=session.session_id,
            event_type=SESSION_CREATED,
            sequence=0,
            payload=_created_payload(session),
        )
        store.commit()
    except ControlError:
        store.rollback()
        raise
    except Exception:
        store.rollback()
        logger.exception(
            "session commit failed",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        raise
    store.after_commit(publisher, tenant_id=tenant_id, session_id=session.session_id)
    credential, expires_at = issue_signaling_credential(
        secret,
        tenant_id=tenant_id,
        session_id=session.session_id,
        channel_tenant_id=session.channel_tenant_id,
        channel_id=session.channel_id,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    logger.info(
        "session created",
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        user_id=user_id,
        outbox_id=event_id,
    )
    return IssuedSession(
        session=session,
        signaling_credential=credential,
        expires_at=expires_at,
    )


def join_session(
    store: ControlStore,
    *,
    tenant_id: int,
    user_id: int,
    session_id: int,
    now: datetime,
    secret: str,
    ttl_seconds: int,
) -> IssuedSession:
    """Mint a new signaling credential for an active session in this tenant."""
    logger.debug(
        "session join started",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )
    try:
        _require_tenant(store, "session join rejected", tenant_id, user_id, session_id)
        session = store.get_session(tenant_id, session_id)
        if session is None:
            _reject(
                "session join rejected",
                "session does not exist",
                404,
                reason="session_missing",
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
            )
        if session.status != "active":
            _reject(
                "session join rejected",
                "session has ended",
                409,
                reason="session_ended",
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
            )
    except ControlError:
        store.rollback()
        raise
    except Exception:
        store.rollback()
        logger.exception(
            "session join failed",
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
        raise
    store.rollback()
    credential, expires_at = issue_signaling_credential(
        secret,
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        channel_tenant_id=session.channel_tenant_id,
        channel_id=session.channel_id,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    logger.info(
        "session joined",
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        user_id=user_id,
    )
    return IssuedSession(
        session=session,
        signaling_credential=credential,
        expires_at=expires_at,
    )


def end_session(
    store: ControlStore,
    *,
    tenant_id: int,
    session_id: int,
    now: datetime,
    reason: EndReason,
    user_id: int | None = None,
    publisher: EventPublisher | None = None,
) -> SessionView:
    """End an active session and write one session.ended event."""
    logger.debug(
        "session end started",
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        reason=reason,
    )
    try:
        _require_tenant(store, "session end rejected", tenant_id, user_id, session_id)
        session = store.lock_session(tenant_id, session_id)
        if session is None:
            _reject(
                "session end rejected",
                "session does not exist",
                404,
                reason="session_missing",
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
            )
        if session.status != "active":
            _reject(
                "session end rejected",
                "session is already ended",
                409,
                reason="session_already_ended",
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
            )
        store.mark_ended(tenant_id, session_id, now)
        event_id = store.add_outbox(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=SESSION_ENDED,
            sequence=store.next_outbox_sequence(tenant_id, session_id),
            payload=_ended_payload(session, now, reason),
        )
        store.commit()
    except ControlError:
        store.rollback()
        raise
    except Exception:
        store.rollback()
        logger.exception(
            "session end failed",
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
        )
        raise
    store.after_commit(publisher, tenant_id=tenant_id, session_id=session_id)
    logger.info(
        "session ended",
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        user_id=user_id,
        outbox_id=event_id,
        reason=reason,
    )
    return SessionView(
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        channel_tenant_id=session.channel_tenant_id,
        channel_id=session.channel_id,
        status="ended",
        started_at=session.started_at,
        ended_at=now,
        description=session.description,
    )


def end_idle_sessions(
    store: ControlStore,
    presence: PresenceBook,
    *,
    idle_seconds: int,
    now: datetime,
    publisher: EventPublisher | None = None,
    grant_unseen: bool = False,
    process_started_at: datetime | None = None,
) -> list[int]:
    """End active sessions that have had no peer for the idle interval."""
    ended: list[int] = []
    for session in store.list_active():
        if not presence.ready_to_end(
            session.tenant_id,
            session.session_id,
            session.started_at,
            now,
            idle_seconds,
            grant_unseen=grant_unseen,
            process_started_at=process_started_at,
        ):
            continue
        logger.debug(
            "session idle candidate",
            tenant_id=session.tenant_id,
            session_id=session.session_id,
        )
        try:
            end_session(
                store,
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                now=now,
                reason="idle",
                publisher=publisher,
            )
        except ControlError as error:
            logger.warning(
                "session idle end skipped",
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                error=str(error),
            )
            continue
        ended.append(session.session_id)
    return ended


def _require_tenant(
    store: ControlStore,
    msg: str,
    tenant_id: int,
    user_id: int | None,
    session_id: int | None,
) -> None:
    if store.tenant_exists(tenant_id):
        return
    _reject(
        msg,
        "tenant does not exist",
        401,
        reason="tenant_missing",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )


def _reject(
    msg: str,
    message: str,
    status_code: int,
    *,
    reason: str,
    tenant_id: int,
    user_id: int | None = None,
    session_id: int | None = None,
) -> NoReturn:
    details: dict[str, object] = {"reason": reason, "tenant_id": tenant_id}
    if user_id is not None:
        details["user_id"] = user_id
    if session_id is not None:
        details["session_id"] = session_id
    logger.debug(msg, **details)
    logged = {key: value for key, value in details.items() if key != "reason"}
    logger.error(msg, error=message, **logged)
    raise ControlError(message, status_code=status_code)


def _description(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _created_payload(session: SessionView) -> dict[str, object]:
    return {
        "tenant_id": session.tenant_id,
        "session_id": session.session_id,
        "channel_tenant_id": session.channel_tenant_id,
        "channel_id": session.channel_id,
        "started_at": session.started_at.isoformat(),
    }


def _ended_payload(session: SessionView, ended_at: datetime, reason: str) -> dict[str, object]:
    return {
        "tenant_id": session.tenant_id,
        "session_id": session.session_id,
        "channel_tenant_id": session.channel_tenant_id,
        "channel_id": session.channel_id,
        "ended_at": ended_at.isoformat(),
        "reason": reason,
    }


def _session_view(row: Any) -> SessionView:
    started_at = row["started_at"]
    ended_at = row["ended_at"]
    description = row["session_description"]
    if not isinstance(started_at, datetime):
        raise TypeError("session started_at is not a timestamp")
    if ended_at is not None and not isinstance(ended_at, datetime):
        raise TypeError("session ended_at is not a timestamp")
    if description is not None and not isinstance(description, str):
        raise TypeError("session description is not text")
    return SessionView(
        tenant_id=int(row["tenant_id"]),
        session_id=int(row["session_id"]),
        channel_tenant_id=int(row["channel_tenant_id"]),
        channel_id=int(row["channel_id"]),
        status=str(row["status"]),
        started_at=started_at,
        ended_at=ended_at,
        description=description,
    )


class SqlControlStore:
    """SQLAlchemy Core persistence for session control."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def tenant_exists(self, tenant_id: int) -> bool:
        """Return whether the tenant row exists."""
        tenants = _table(Tenant)
        found = self._connection.execute(
            select(tenants.c.tenant_id).where(tenants.c.tenant_id == tenant_id)
        ).scalar_one_or_none()
        return found is not None

    def find_channel(self, channel_id: int) -> tuple[int, int] | None:
        """Return channel tenant and channel id, or none when the row is missing."""
        channels = _table(Channel)
        row = self._connection.execute(
            select(channels.c.tenant_id, channels.c.channel_id).where(
                channels.c.channel_id == channel_id
            )
        ).first()
        if row is None:
            return None
        return int(row.tenant_id), int(row.channel_id)

    def add_session(
        self,
        *,
        tenant_id: int,
        channel_tenant_id: int,
        channel_id: int,
        description: str | None,
        started_at: datetime,
    ) -> SessionView:
        """Stage an active session and return it with its new id."""
        sessions = _table(Session)
        session_id = self._connection.execute(
            insert(sessions)
            .values(
                tenant_id=tenant_id,
                channel_tenant_id=channel_tenant_id,
                channel_id=channel_id,
                session_description=description,
                status="active",
                started_at=started_at,
            )
            .returning(sessions.c.session_id)
        ).scalar_one()
        return SessionView(
            tenant_id=tenant_id,
            session_id=int(session_id),
            channel_tenant_id=channel_tenant_id,
            channel_id=channel_id,
            status="active",
            started_at=started_at,
            ended_at=None,
            description=description,
        )

    def add_outbox(
        self,
        *,
        tenant_id: int,
        session_id: int,
        event_type: str,
        sequence: int,
        payload: dict[str, object],
    ) -> int:
        """Stage one pending outbox row for the current transaction."""
        return insert_outbox_event(
            self._connection,
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type,
            sequence=sequence,
            payload=payload,
        )

    def get_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Return the session in this tenant, or none."""
        return self._load(tenant_id, session_id, lock=False)

    def lock_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Lock the session row in this tenant and return it, or none."""
        return self._load(tenant_id, session_id, lock=True)

    def mark_ended(self, tenant_id: int, session_id: int, ended_at: datetime) -> None:
        """Stage status ended and ended_at on the locked session."""
        sessions = _table(Session)
        result = self._connection.execute(
            update(sessions)
            .where(
                sessions.c.tenant_id == tenant_id,
                sessions.c.session_id == session_id,
                sessions.c.status == "active",
            )
            .values(status="ended", ended_at=ended_at)
        )
        if result.rowcount != 1:
            raise RuntimeError("session is not locked")

    def next_outbox_sequence(self, tenant_id: int, session_id: int) -> int:
        """Return the next per-session outbox sequence."""
        outbox = _table(OutboxEvent)
        allocated = self._connection.execute(
            select(func.coalesce(func.max(outbox.c.sequence), -1) + 1).where(
                outbox.c.tenant_id == tenant_id,
                outbox.c.session_id == session_id,
            )
        ).scalar_one()
        return int(allocated)

    def commit(self) -> None:
        """Commit the staged session change and outbox row together."""
        self._connection.commit()

    def rollback(self) -> None:
        """Drop the staged session change and outbox row."""
        if self._connection.in_transaction():
            self._connection.rollback()

    def after_commit(
        self,
        publisher: EventPublisher | None,
        *,
        tenant_id: int,
        session_id: int,
    ) -> None:
        """Publish the committed session event. A bus failure leaves it pending."""
        if publisher is None:
            return
        try:
            publish_pending(
                DatabaseOutboxStore(
                    self._connection,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ),
                publisher,
            )
        except Exception:
            logger.warning(
                "session event not published",
                tenant_id=tenant_id,
                session_id=session_id,
                exc_info=True,
            )

    def list_active(self) -> list[SessionView]:
        """Return active sessions across tenants."""
        sessions = _table(Session)
        rows = (
            self._connection.execute(
                select(
                    sessions.c.tenant_id,
                    sessions.c.session_id,
                    sessions.c.channel_tenant_id,
                    sessions.c.channel_id,
                    sessions.c.status,
                    sessions.c.started_at,
                    sessions.c.ended_at,
                    sessions.c.session_description,
                )
                .where(sessions.c.status == "active")
                .order_by(sessions.c.tenant_id, sessions.c.session_id)
            )
            .mappings()
            .all()
        )
        return [_session_view(row) for row in rows]

    def _load(self, tenant_id: int, session_id: int, *, lock: bool) -> SessionView | None:
        sessions = _table(Session)
        statement = select(
            sessions.c.tenant_id,
            sessions.c.session_id,
            sessions.c.channel_tenant_id,
            sessions.c.channel_id,
            sessions.c.status,
            sessions.c.started_at,
            sessions.c.ended_at,
            sessions.c.session_description,
        ).where(
            sessions.c.tenant_id == tenant_id,
            sessions.c.session_id == session_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = self._connection.execute(statement).mappings().first()
        if row is None:
            return None
        return _session_view(row)
