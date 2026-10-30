"""Mapped classes for the ceho_core schema.

Ids are bigint identity columns. tenant_id is a foreign key, not part of the
primary key. Child rows also reference (tenant_id, parent id) so a session,
participant, or transcript cannot point at another tenant.

Tenant 1 is the generic tenant. A row stored there is global to all tenants.
channels is that catalog (WebRTC, Slack, Teams, and so on): a session may use
a channel owned by its own tenant or by the generic tenant. A channel owned
by any other tenant stays private to that tenant.

A user is a platform login account. A speaker is a voice that can be
attributed and may point at one user after that person confirms it. A session
speaker is one voice inside one session. A connection, a user, and a session
can each have many session speakers.

Transcript identity is (session_id, turn_index). turn_index repeats in every
session, so it cannot be a primary key by itself. A transcript may also name
the media object and millisecond interval that support that turn.

Media objects store server-owned S3 locations for source audio. Outbox events
live in this schema so a domain write and its event can commit together. The
outbox identity and payload are immutable after insert.

The design DDL in docs/technical/architecture/ceho_core/ddl matches these
classes. These classes are the schema source of truth.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "ceho_core"

# Rows owned by this tenant are visible to every tenant.
GENERIC_TENANT_ID = 1

SET_UPDATED_AT_FUNCTION = """
CREATE OR REPLACE FUNCTION ceho_core.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
"""

REJECT_OUTBOX_EVENT_MUTATION_FUNCTION = """
CREATE OR REPLACE FUNCTION ceho_core.reject_outbox_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.event_type IS DISTINCT FROM OLD.event_type
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.sequence IS DISTINCT FROM OLD.sequence
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'outbox event identity and payload are immutable';
    END IF;
    RETURN NEW;
END;
$$;
"""

OUTBOX_EVENT_IMMUTABLE_TRIGGER = """
CREATE TRIGGER outbox_events_reject_mutation
    BEFORE UPDATE ON ceho_core.outbox_events
    FOR EACH ROW EXECUTE FUNCTION ceho_core.reject_outbox_event_mutation()
"""


def updated_at_trigger_statements(table_names: tuple[str, ...]) -> tuple[str, ...]:
    """Build BEFORE UPDATE triggers for the named tables.

    Pass only the tables the current Alembic revision creates. A revision that
    creates ``tenants`` must not also create triggers for later tables.
    """
    return tuple(
        f"""
        CREATE TRIGGER {table_name}_set_updated_at
            BEFORE UPDATE ON ceho_core.{table_name}
            FOR EACH ROW EXECUTE FUNCTION ceho_core.set_updated_at()
        """.strip()
        for table_name in table_names
    )


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


def _identity() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


class Tenant(Timestamped, Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[int] = _identity()
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Channel(Timestamped, Base):
    __tablename__ = "channels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.tenants.tenant_id"],
            name="channels_tenant_id_fkey",
        ),
        UniqueConstraint("tenant_id", "channel_id", name="channels_tenant_channel_unique"),
        UniqueConstraint("tenant_id", "channel_name", name="channels_tenant_name_unique"),
    )

    channel_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_name: Mapped[str] = mapped_column(Text, nullable=False)


class User(Timestamped, Base):
    """Platform login account. Authentication secrets stay out of this table."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(external_ids) = 'object'",
            name="users_external_ids_object_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.tenants.tenant_id"],
            name="users_tenant_id_fkey",
        ),
        UniqueConstraint("tenant_id", "user_id", name="users_tenant_user_unique"),
        # GIN supports containment lookups such as provider -> subject.
        Index("users_external_ids_gin_idx", "external_ids", postgresql_using="gin"),
    )

    user_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    external_ids: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )


class Speaker(Timestamped, Base):
    """A voice that can be attributed, optionally confirmed as one platform user."""

    __tablename__ = "speakers"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(external_ids) = 'object'",
            name="speakers_external_ids_object_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.tenants.tenant_id"],
            name="speakers_tenant_id_fkey",
        ),
        # SET NULL (user_id) drops the account link and leaves the voice in place.
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            [f"{SCHEMA}.users.tenant_id", f"{SCHEMA}.users.user_id"],
            name="speakers_tenant_user_fkey",
            ondelete="SET NULL (user_id)",
        ),
        UniqueConstraint("tenant_id", "speaker_id", name="speakers_tenant_speaker_unique"),
        Index("speakers_external_ids_gin_idx", "external_ids", postgresql_using="gin"),
        Index("speakers_user_id_idx", "user_id"),
    )

    speaker_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    external_ids: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger)


class Session(Timestamped, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'ended')", name="sessions_status_check"),
        CheckConstraint(
            "(status = 'active' AND ended_at IS NULL) "
            "OR (status = 'ended' AND ended_at IS NOT NULL AND ended_at >= started_at)",
            name="sessions_ended_timestamp_check",
        ),
        # A session uses its own channel, or a channel of the generic tenant.
        CheckConstraint(
            f"channel_tenant_id = tenant_id OR channel_tenant_id = {GENERIC_TENANT_ID}",
            name="sessions_channel_tenant_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.tenants.tenant_id"],
            name="sessions_tenant_id_fkey",
        ),
        ForeignKeyConstraint(
            ["channel_tenant_id", "channel_id"],
            [f"{SCHEMA}.channels.tenant_id", f"{SCHEMA}.channels.channel_id"],
            name="sessions_tenant_channel_fkey",
        ),
        UniqueConstraint("tenant_id", "session_id", name="sessions_tenant_session_unique"),
        Index("sessions_channel_idx", "channel_tenant_id", "channel_id"),
    )

    session_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionSpeaker(Timestamped, Base):
    """One voice inside one session. Several can share a connection or a user."""

    __tablename__ = "session_speakers"
    __table_args__ = (
        CheckConstraint(
            "label IS NULL OR btrim(label) <> ''",
            name="session_speakers_label_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            [f"{SCHEMA}.sessions.tenant_id", f"{SCHEMA}.sessions.session_id"],
            name="session_speakers_tenant_session_fkey",
            ondelete="CASCADE",
        ),
        # SET NULL (speaker_id) drops the voice link and leaves the session row.
        ForeignKeyConstraint(
            ["tenant_id", "speaker_id"],
            [f"{SCHEMA}.speakers.tenant_id", f"{SCHEMA}.speakers.speaker_id"],
            name="session_speakers_tenant_speaker_fkey",
            ondelete="SET NULL (speaker_id)",
        ),
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "session_speaker_id",
            name="session_speakers_tenant_session_speaker_unique",
        ),
        # A known speaker joins a session once. PostgreSQL treats null
        # speaker_id values as distinct, so unlinked voices can repeat.
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "speaker_id",
            name="session_speakers_known_speaker_unique",
        ),
        # A session-local label such as speaker_1 is used once. Null labels repeat.
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "label",
            name="session_speakers_label_unique",
        ),
        Index("session_speakers_speaker_id_idx", "speaker_id"),
    )

    session_speaker_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    speaker_id: Mapped[int | None] = mapped_column(BigInteger)
    label: Mapped[str | None] = mapped_column(Text)


class Transcript(Timestamped, Base):
    __tablename__ = "transcripts"
    __table_args__ = (
        CheckConstraint("turn_index >= 0", name="transcripts_turn_index_check"),
        CheckConstraint("end_at >= start_at", name="transcripts_time_check"),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            [f"{SCHEMA}.sessions.tenant_id", f"{SCHEMA}.sessions.session_id"],
            name="transcripts_tenant_session_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id", "session_speaker_id"],
            [
                f"{SCHEMA}.session_speakers.tenant_id",
                f"{SCHEMA}.session_speakers.session_id",
                f"{SCHEMA}.session_speakers.session_speaker_id",
            ],
            name="transcripts_speaker_fkey",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(media_object_id IS NULL AND media_start_offset_ms IS NULL "
            "AND media_end_offset_ms IS NULL) OR ("
            "media_object_id IS NOT NULL AND media_start_offset_ms IS NOT NULL "
            "AND media_end_offset_ms IS NOT NULL AND media_start_offset_ms >= 0 "
            "AND media_end_offset_ms >= media_start_offset_ms)",
            name="transcripts_media_provenance_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id", "media_object_id"],
            [
                f"{SCHEMA}.media_objects.tenant_id",
                f"{SCHEMA}.media_objects.session_id",
                f"{SCHEMA}.media_objects.media_object_id",
            ],
            name="transcripts_media_object_fkey",
            ondelete="RESTRICT",
        ),
        Index("transcripts_session_speaker_id_idx", "session_id", "session_speaker_id"),
    )

    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    turn_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_speaker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    media_object_id: Mapped[int | None] = mapped_column(BigInteger)
    media_start_offset_ms: Mapped[int | None] = mapped_column(BigInteger)
    media_end_offset_ms: Mapped[int | None] = mapped_column(BigInteger)


class MediaObject(Timestamped, Base):
    """Recorded source audio owned by one tenant and session."""

    __tablename__ = "media_objects"
    __table_args__ = (
        CheckConstraint(
            "write_status IN ('open', 'complete', 'failed')",
            name="media_objects_write_status_check",
        ),
        CheckConstraint(
            "retention_status IN ('active', 'deleted')",
            name="media_objects_retention_status_check",
        ),
        CheckConstraint(
            "(retention_status = 'active' AND deleted_at IS NULL) OR "
            "(retention_status = 'deleted' AND deleted_at IS NOT NULL)",
            name="media_objects_deleted_at_check",
        ),
        CheckConstraint(
            "sample_rate_hz IS NULL OR sample_rate_hz > 0",
            name="media_objects_sample_rate_check",
        ),
        CheckConstraint(
            "channel_count IS NULL OR channel_count > 0",
            name="media_objects_channel_count_check",
        ),
        CheckConstraint(
            "byte_length IS NULL OR byte_length >= 0",
            name="media_objects_byte_length_check",
        ),
        CheckConstraint(
            "media_started_at IS NULL OR media_ended_at IS NULL "
            "OR media_ended_at >= media_started_at",
            name="media_objects_interval_check",
        ),
        CheckConstraint(
            "write_status <> 'complete' OR ("
            "content_type IS NOT NULL AND codec IS NOT NULL AND sample_rate_hz IS NOT NULL "
            "AND channel_count IS NOT NULL AND media_started_at IS NOT NULL "
            "AND media_ended_at IS NOT NULL AND byte_length IS NOT NULL AND checksum IS NOT NULL)",
            name="media_objects_complete_write_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            [f"{SCHEMA}.sessions.tenant_id", f"{SCHEMA}.sessions.session_id"],
            name="media_objects_tenant_session_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "media_object_id",
            name="media_objects_tenant_session_object_unique",
        ),
    )

    media_object_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    codec: Mapped[str | None] = mapped_column(Text)
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    channel_count: Mapped[int | None] = mapped_column(Integer)
    media_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    media_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    byte_length: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(Text)
    write_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    retention_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Timestamped, Base):
    """Durable hot-path event stored in the same database as domain state."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('session.created', 'session.ended', 'transcript.created')",
            name="outbox_events_type_check",
        ),
        CheckConstraint("schema_version >= 1", name="outbox_events_schema_version_check"),
        CheckConstraint("sequence >= 0", name="outbox_events_sequence_check"),
        CheckConstraint("attempt_count >= 0", name="outbox_events_attempt_count_check"),
        CheckConstraint(
            "publication_status IN ('pending', 'published', 'failed')",
            name="outbox_events_publication_status_check",
        ),
        CheckConstraint(
            "(publication_status = 'published' AND published_at IS NOT NULL) OR "
            "(publication_status <> 'published' AND published_at IS NULL)",
            name="outbox_events_published_at_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            [f"{SCHEMA}.sessions.tenant_id", f"{SCHEMA}.sessions.session_id"],
            name="outbox_events_tenant_session_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "sequence",
            name="outbox_events_session_sequence_unique",
        ),
        Index(
            "outbox_events_unpublished_idx",
            "event_id",
            postgresql_where=text("publication_status <> 'published'"),
        ),
    )

    event_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    publication_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
