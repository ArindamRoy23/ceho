"""tighten ceho_core integrity

Revision ID: f9a656e37280
Revises: b6e2a14f9c30
Create Date: 2026-11-02 10:13:11.105317

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from ceho.models import (
    REJECT_GENERIC_CHANNEL_NAME_SHADOW_FUNCTION,
    REJECT_GENERIC_CHANNEL_NAME_SHADOW_TRIGGER,
    REJECT_PENDING_OUTBOX_DELETE_FUNCTION,
    REJECT_PENDING_OUTBOX_DELETE_TRIGGER,
)

# revision identifiers, used by Alembic.
revision: str = "f9a656e37280"
down_revision: str | Sequence[str] | None = "b6e2a14f9c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ceho_core"

_TEXT_CHECKS = (
    ("tenants", "tenants_name_check", "name IS NULL OR btrim(name) <> ''"),
    ("channels", "channels_channel_name_check", "btrim(channel_name) <> ''"),
    (
        "users",
        "users_email_check",
        "email = lower(btrim(email)) AND email ~ '^[^@[:space:]]+@[^@[:space:]]+$'",
    ),
    ("users", "users_display_name_check", "display_name IS NULL OR btrim(display_name) <> ''"),
    (
        "speakers",
        "speakers_display_name_check",
        "display_name IS NULL OR btrim(display_name) <> ''",
    ),
    (
        "sessions",
        "sessions_session_description_check",
        "session_description IS NULL OR btrim(session_description) <> ''",
    ),
    ("transcripts", "transcripts_text_check", "btrim(text) <> ''"),
    ("media_objects", "media_objects_storage_uri_check", "btrim(storage_uri) <> ''"),
    (
        "media_objects",
        "media_objects_content_type_check",
        "content_type IS NULL OR btrim(content_type) <> ''",
    ),
    ("media_objects", "media_objects_codec_check", "codec IS NULL OR btrim(codec) <> ''"),
    (
        "media_objects",
        "media_objects_checksum_check",
        "checksum IS NULL OR btrim(checksum) <> ''",
    ),
)


def upgrade() -> None:
    """Require a login email and close the integrity gaps in ceho_core."""
    op.add_column("users", sa.Column("email", sa.Text(), nullable=False), schema=SCHEMA)
    for table_name, column_name in (
        ("tenants", "name"),
        ("users", "display_name"),
        ("speakers", "display_name"),
    ):
        op.alter_column(
            table_name, column_name, existing_type=sa.Text(), nullable=True, schema=SCHEMA
        )
    for table_name, name, expression in _TEXT_CHECKS:
        op.create_check_constraint(name, table_name, expression, schema=SCHEMA)
    op.create_unique_constraint(
        "users_tenant_email_unique", "users", ["tenant_id", "email"], schema=SCHEMA
    )
    op.create_unique_constraint(
        "media_objects_storage_uri_unique",
        "media_objects",
        ["storage_uri"],
        schema=SCHEMA,
    )
    op.drop_constraint("transcripts_speaker_fkey", "transcripts", schema=SCHEMA, type_="foreignkey")
    op.create_foreign_key(
        "transcripts_speaker_fkey",
        "transcripts",
        "session_speakers",
        ["tenant_id", "session_id", "session_speaker_id"],
        ["tenant_id", "session_id", "session_speaker_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(REJECT_PENDING_OUTBOX_DELETE_FUNCTION)
    op.execute(REJECT_PENDING_OUTBOX_DELETE_TRIGGER)
    op.execute(REJECT_GENERIC_CHANNEL_NAME_SHADOW_FUNCTION)
    op.execute(REJECT_GENERIC_CHANNEL_NAME_SHADOW_TRIGGER)
    # The speakers split left the old names in place. Rename the original
    # users sequence before the suffixed one takes its name.
    op.execute("ALTER SEQUENCE ceho_core.users_user_id_seq RENAME TO speakers_speaker_id_seq")
    op.execute(
        "ALTER SEQUENCE ceho_core.session_users_session_user_id_seq "
        "RENAME TO session_speakers_session_speaker_id_seq"
    )
    op.execute("ALTER SEQUENCE ceho_core.users_user_id_seq1 RENAME TO users_user_id_seq")


def downgrade() -> None:
    """Restore the previous nullability, deletes, and sequence names."""
    op.execute("ALTER SEQUENCE ceho_core.users_user_id_seq RENAME TO users_user_id_seq1")
    op.execute(
        "ALTER SEQUENCE ceho_core.session_speakers_session_speaker_id_seq "
        "RENAME TO session_users_session_user_id_seq"
    )
    op.execute("ALTER SEQUENCE ceho_core.speakers_speaker_id_seq RENAME TO users_user_id_seq")
    op.execute("DROP TRIGGER IF EXISTS channels_reject_generic_name_shadow ON ceho_core.channels")
    op.execute("DROP FUNCTION IF EXISTS ceho_core.reject_generic_channel_name_shadow()")
    op.execute(
        "DROP TRIGGER IF EXISTS outbox_events_reject_pending_delete ON ceho_core.outbox_events"
    )
    op.execute("DROP FUNCTION IF EXISTS ceho_core.reject_pending_outbox_delete()")
    op.drop_constraint("transcripts_speaker_fkey", "transcripts", schema=SCHEMA, type_="foreignkey")
    op.create_foreign_key(
        "transcripts_speaker_fkey",
        "transcripts",
        "session_speakers",
        ["tenant_id", "session_id", "session_speaker_id"],
        ["tenant_id", "session_id", "session_speaker_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "media_objects_storage_uri_unique",
        "media_objects",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint("users_tenant_email_unique", "users", schema=SCHEMA, type_="unique")
    for table_name, name, _expression in reversed(_TEXT_CHECKS):
        op.drop_constraint(name, table_name, schema=SCHEMA, type_="check")
    for table_name, column_name in (
        ("speakers", "display_name"),
        ("users", "display_name"),
        ("tenants", "name"),
    ):
        op.execute(
            f"UPDATE {SCHEMA}.{table_name} SET {column_name} = 'unnamed' "
            f"WHERE {column_name} IS NULL"
        )
        op.alter_column(
            table_name, column_name, existing_type=sa.Text(), nullable=False, schema=SCHEMA
        )
    op.drop_column("users", "email", schema=SCHEMA)
