"""Mapped classes for the ceho_core schema.

Ids are bigint identity columns. tenant_id is a foreign key, not part of the
primary key. Child rows also reference (tenant_id, parent id) so a session,
participant, or transcript cannot point at another tenant.

Tenant 1 is the generic tenant. A row stored there is global to all tenants.
channels is that catalog (WebRTC, Slack, Teams, and so on): a session may use
a channel owned by its own tenant or by the generic tenant. A channel owned
by any other tenant stays private to that tenant.

Transcript identity is (session_id, turn_index). turn_index repeats in every
session, so it cannot be a primary key by itself.

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
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
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

UPDATED_AT_TABLES = (
    "tenants",
    "channels",
    "users",
    "sessions",
    "session_users",
    "transcripts",
)


def updated_at_trigger_statements() -> tuple[str, ...]:
    return tuple(
        f"""
        CREATE TRIGGER {table_name}_set_updated_at
            BEFORE UPDATE ON ceho_core.{table_name}
            FOR EACH ROW EXECUTE FUNCTION ceho_core.set_updated_at()
        """.strip()
        for table_name in UPDATED_AT_TABLES
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
    __tablename__ = "users"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.tenants.tenant_id"],
            name="users_tenant_id_fkey",
        ),
        UniqueConstraint("tenant_id", "user_id", name="users_tenant_user_unique"),
        UniqueConstraint("tenant_id", "external_id", name="users_tenant_external_id_unique"),
    )

    user_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)


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


class SessionUser(Timestamped, Base):
    __tablename__ = "session_users"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            [f"{SCHEMA}.sessions.tenant_id", f"{SCHEMA}.sessions.session_id"],
            name="session_users_tenant_session_fkey",
            ondelete="CASCADE",
        ),
        # SET NULL (user_id) drops the speaker link and leaves tenant_id in place.
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            [f"{SCHEMA}.users.tenant_id", f"{SCHEMA}.users.user_id"],
            name="session_users_tenant_user_fkey",
            ondelete="SET NULL (user_id)",
        ),
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "session_user_id",
            name="session_users_tenant_session_speaker_unique",
        ),
        Index("session_users_user_id_idx", "user_id"),
    )

    session_user_id: Mapped[int] = _identity()
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger)


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
            ["tenant_id", "session_id", "session_user_id"],
            [
                f"{SCHEMA}.session_users.tenant_id",
                f"{SCHEMA}.session_users.session_id",
                f"{SCHEMA}.session_users.session_user_id",
            ],
            name="transcripts_speaker_fkey",
            ondelete="CASCADE",
        ),
        Index("transcripts_session_user_id_idx", "session_id", "session_user_id"),
    )

    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    turn_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
