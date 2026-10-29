"""add direct-source hot-path schema

Revision ID: 2014c8c3f1c5
Revises: 174efb5dc4b5
Create Date: 2026-10-29 12:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from ceho.models import (
    OUTBOX_EVENT_IMMUTABLE_TRIGGER,
    REJECT_OUTBOX_EVENT_MUTATION_FUNCTION,
    updated_at_trigger_statements,
)

# revision identifiers, used by Alembic.
revision: str = "2014c8c3f1c5"
down_revision: str | Sequence[str] | None = "174efb5dc4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column(
            "external_ids",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="ceho_core",
    )
    op.execute(
        """
        UPDATE ceho_core.users
        SET external_ids = jsonb_build_object('default', external_id)
        WHERE external_id IS NOT NULL AND external_id <> ''
        """
    )
    op.create_check_constraint(
        "users_external_ids_object_check",
        "users",
        "jsonb_typeof(external_ids) = 'object'",
        schema="ceho_core",
    )
    op.create_index(
        "users_external_ids_gin_idx",
        "users",
        ["external_ids"],
        unique=False,
        schema="ceho_core",
        postgresql_using="gin",
    )
    op.drop_constraint(
        "users_tenant_external_id_unique",
        "users",
        schema="ceho_core",
        type_="unique",
    )
    op.drop_column("users", "external_id", schema="ceho_core")
    # Null user_id values stay distinct, so anonymous memberships can repeat.
    op.create_unique_constraint(
        "session_users_tenant_session_user_unique",
        "session_users",
        ["tenant_id", "session_id", "user_id"],
        schema="ceho_core",
    )
    op.create_table(
        "media_objects",
        sa.Column("media_object_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("codec", sa.Text(), nullable=True),
        sa.Column("sample_rate_hz", sa.Integer(), nullable=True),
        sa.Column("channel_count", sa.Integer(), nullable=True),
        sa.Column("media_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("media_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("byte_length", sa.BigInteger(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column("write_status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column(
            "retention_status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "write_status IN ('open', 'complete', 'failed')",
            name="media_objects_write_status_check",
        ),
        sa.CheckConstraint(
            "retention_status IN ('active', 'deleted')",
            name="media_objects_retention_status_check",
        ),
        sa.CheckConstraint(
            "(retention_status = 'active' AND deleted_at IS NULL) OR "
            "(retention_status = 'deleted' AND deleted_at IS NOT NULL)",
            name="media_objects_deleted_at_check",
        ),
        sa.CheckConstraint(
            "sample_rate_hz IS NULL OR sample_rate_hz > 0",
            name="media_objects_sample_rate_check",
        ),
        sa.CheckConstraint(
            "channel_count IS NULL OR channel_count > 0",
            name="media_objects_channel_count_check",
        ),
        sa.CheckConstraint(
            "byte_length IS NULL OR byte_length >= 0",
            name="media_objects_byte_length_check",
        ),
        sa.CheckConstraint(
            "media_started_at IS NULL OR media_ended_at IS NULL "
            "OR media_ended_at >= media_started_at",
            name="media_objects_interval_check",
        ),
        sa.CheckConstraint(
            "write_status <> 'complete' OR ("
            "content_type IS NOT NULL AND codec IS NOT NULL AND sample_rate_hz IS NOT NULL "
            "AND channel_count IS NOT NULL AND media_started_at IS NOT NULL "
            "AND media_ended_at IS NOT NULL AND byte_length IS NOT NULL AND checksum IS NOT NULL)",
            name="media_objects_complete_write_check",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["ceho_core.sessions.tenant_id", "ceho_core.sessions.session_id"],
            name="media_objects_tenant_session_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("media_object_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "media_object_id",
            name="media_objects_tenant_session_object_unique",
        ),
        schema="ceho_core",
    )
    for statement in updated_at_trigger_statements(("media_objects",)):
        op.execute(statement)
    op.add_column(
        "transcripts",
        sa.Column("media_object_id", sa.BigInteger(), nullable=True),
        schema="ceho_core",
    )
    op.add_column(
        "transcripts",
        sa.Column("media_start_offset_ms", sa.BigInteger(), nullable=True),
        schema="ceho_core",
    )
    op.add_column(
        "transcripts",
        sa.Column("media_end_offset_ms", sa.BigInteger(), nullable=True),
        schema="ceho_core",
    )
    op.create_check_constraint(
        "transcripts_media_provenance_check",
        "transcripts",
        "(media_object_id IS NULL AND media_start_offset_ms IS NULL "
        "AND media_end_offset_ms IS NULL) OR ("
        "media_object_id IS NOT NULL AND media_start_offset_ms IS NOT NULL "
        "AND media_end_offset_ms IS NOT NULL AND media_start_offset_ms >= 0 "
        "AND media_end_offset_ms >= media_start_offset_ms)",
        schema="ceho_core",
    )
    op.create_foreign_key(
        "transcripts_media_object_fkey",
        "transcripts",
        "media_objects",
        ["tenant_id", "session_id", "media_object_id"],
        ["tenant_id", "session_id", "media_object_id"],
        source_schema="ceho_core",
        referent_schema="ceho_core",
        ondelete="RESTRICT",
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "publication_status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('session.created', 'session.ended', 'transcript.created')",
            name="outbox_events_type_check",
        ),
        sa.CheckConstraint("schema_version >= 1", name="outbox_events_schema_version_check"),
        sa.CheckConstraint("sequence >= 0", name="outbox_events_sequence_check"),
        sa.CheckConstraint("attempt_count >= 0", name="outbox_events_attempt_count_check"),
        sa.CheckConstraint(
            "publication_status IN ('pending', 'published', 'failed')",
            name="outbox_events_publication_status_check",
        ),
        sa.CheckConstraint(
            "(publication_status = 'published' AND published_at IS NOT NULL) OR "
            "(publication_status <> 'published' AND published_at IS NULL)",
            name="outbox_events_published_at_check",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["ceho_core.sessions.tenant_id", "ceho_core.sessions.session_id"],
            name="outbox_events_tenant_session_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "sequence",
            name="outbox_events_session_sequence_unique",
        ),
        schema="ceho_core",
    )
    op.create_index(
        "outbox_events_unpublished_idx",
        "outbox_events",
        ["event_id"],
        unique=False,
        schema="ceho_core",
        postgresql_where=sa.text("publication_status <> 'published'"),
    )
    for statement in updated_at_trigger_statements(("outbox_events",)):
        op.execute(statement)
    op.execute(REJECT_OUTBOX_EVENT_MUTATION_FUNCTION)
    op.execute(OUTBOX_EVENT_IMMUTABLE_TRIGGER)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS outbox_events_reject_mutation ON ceho_core.outbox_events")
    op.execute("DROP TRIGGER IF EXISTS outbox_events_set_updated_at ON ceho_core.outbox_events")
    op.drop_index(
        "outbox_events_unpublished_idx",
        table_name="outbox_events",
        schema="ceho_core",
    )
    op.drop_table("outbox_events", schema="ceho_core")
    op.execute("DROP FUNCTION IF EXISTS ceho_core.reject_outbox_event_mutation()")
    op.drop_constraint(
        "transcripts_media_object_fkey",
        "transcripts",
        schema="ceho_core",
        type_="foreignkey",
    )
    op.drop_constraint(
        "transcripts_media_provenance_check",
        "transcripts",
        schema="ceho_core",
        type_="check",
    )
    op.drop_column("transcripts", "media_end_offset_ms", schema="ceho_core")
    op.drop_column("transcripts", "media_start_offset_ms", schema="ceho_core")
    op.drop_column("transcripts", "media_object_id", schema="ceho_core")
    op.execute("DROP TRIGGER IF EXISTS media_objects_set_updated_at ON ceho_core.media_objects")
    op.drop_table("media_objects", schema="ceho_core")
    op.drop_constraint(
        "session_users_tenant_session_user_unique",
        "session_users",
        schema="ceho_core",
        type_="unique",
    )
    op.add_column(
        "users",
        sa.Column("external_id", sa.Text(), nullable=True),
        schema="ceho_core",
    )
    op.execute(
        """
        UPDATE ceho_core.users
        SET external_id = COALESCE(external_ids ->> 'default', '')
        """
    )
    op.alter_column(
        "users",
        "external_id",
        existing_type=sa.Text(),
        nullable=False,
        schema="ceho_core",
    )
    op.create_unique_constraint(
        "users_tenant_external_id_unique",
        "users",
        ["tenant_id", "external_id"],
        schema="ceho_core",
    )
    op.drop_index("users_external_ids_gin_idx", table_name="users", schema="ceho_core")
    op.drop_constraint(
        "users_external_ids_object_check",
        "users",
        schema="ceho_core",
        type_="check",
    )
    op.drop_column("users", "external_ids", schema="ceho_core")
