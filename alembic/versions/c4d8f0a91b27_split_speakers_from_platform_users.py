"""split speakers from platform users

Revision ID: c4d8f0a91b27
Revises: 2014c8c3f1c5
Create Date: 2026-10-30 15:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from ceho.models import updated_at_trigger_statements

# revision identifiers, used by Alembic.
revision: str = "c4d8f0a91b27"
down_revision: str | Sequence[str] | None = "2014c8c3f1c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ceho_core"


def upgrade() -> None:
    """Move voice rows to speakers and keep users for platform login."""
    _rename_table("users", "speakers")
    _rename_column("speakers", "user_id", "speaker_id")
    _rename_index_and_constraint("speakers", "users_pkey", "speakers_pkey")
    _rename_index_and_constraint(
        "speakers",
        "users_tenant_user_unique",
        "speakers_tenant_speaker_unique",
    )
    _rename_constraint("speakers", "users_tenant_id_fkey", "speakers_tenant_id_fkey")
    _rename_constraint(
        "speakers",
        "users_external_ids_object_check",
        "speakers_external_ids_object_check",
    )
    _rename_index("users_external_ids_gin_idx", "speakers_external_ids_gin_idx")
    _rename_trigger("speakers", "users_set_updated_at", "speakers_set_updated_at")

    _rename_table("session_users", "session_speakers")
    _rename_column("session_speakers", "session_user_id", "session_speaker_id")
    _rename_column("session_speakers", "user_id", "speaker_id")
    _rename_index_and_constraint(
        "session_speakers",
        "session_users_pkey",
        "session_speakers_pkey",
    )
    _rename_index_and_constraint(
        "session_speakers",
        "session_users_tenant_session_speaker_unique",
        "session_speakers_tenant_session_speaker_unique",
    )
    _rename_index_and_constraint(
        "session_speakers",
        "session_users_tenant_session_user_unique",
        "session_speakers_known_speaker_unique",
    )
    _rename_constraint(
        "session_speakers",
        "session_users_tenant_session_fkey",
        "session_speakers_tenant_session_fkey",
    )
    _rename_constraint(
        "session_speakers",
        "session_users_tenant_user_fkey",
        "session_speakers_tenant_speaker_fkey",
    )
    _rename_index("session_users_user_id_idx", "session_speakers_speaker_id_idx")
    _rename_trigger(
        "session_speakers",
        "session_users_set_updated_at",
        "session_speakers_set_updated_at",
    )

    _rename_column("transcripts", "session_user_id", "session_speaker_id")
    _rename_index(
        "transcripts_session_user_id_idx",
        "transcripts_session_speaker_id_idx",
    )

    op.create_table(
        "users",
        sa.Column("user_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "external_ids",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            "jsonb_typeof(external_ids) = 'object'",
            name="users_external_ids_object_check",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["ceho_core.tenants.tenant_id"],
            name="users_tenant_id_fkey",
        ),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("tenant_id", "user_id", name="users_tenant_user_unique"),
        schema=SCHEMA,
    )
    op.create_index(
        "users_external_ids_gin_idx",
        "users",
        ["external_ids"],
        unique=False,
        schema=SCHEMA,
        postgresql_using="gin",
    )
    for statement in updated_at_trigger_statements(("users",)):
        op.execute(statement)

    op.add_column(
        "speakers",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "speakers_tenant_user_fkey",
        "speakers",
        "users",
        ["tenant_id", "user_id"],
        ["tenant_id", "user_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL (user_id)",
    )
    op.create_index(
        "speakers_user_id_idx",
        "speakers",
        ["user_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.add_column(
        "session_speakers",
        sa.Column("label", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "session_speakers_label_check",
        "session_speakers",
        "label IS NULL OR btrim(label) <> ''",
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "session_speakers_label_unique",
        "session_speakers",
        ["tenant_id", "session_id", "label"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Restore voice rows to the users and session_users tables."""
    op.drop_constraint(
        "session_speakers_label_unique",
        "session_speakers",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "session_speakers_label_check",
        "session_speakers",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("session_speakers", "label", schema=SCHEMA)
    op.drop_index("speakers_user_id_idx", table_name="speakers", schema=SCHEMA)
    op.drop_constraint(
        "speakers_tenant_user_fkey",
        "speakers",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("speakers", "user_id", schema=SCHEMA)
    op.execute("DROP TRIGGER IF EXISTS users_set_updated_at ON ceho_core.users")
    op.drop_index("users_external_ids_gin_idx", table_name="users", schema=SCHEMA)
    op.drop_table("users", schema=SCHEMA)

    _rename_index(
        "transcripts_session_speaker_id_idx",
        "transcripts_session_user_id_idx",
    )
    _rename_column("transcripts", "session_speaker_id", "session_user_id")

    _rename_trigger(
        "session_speakers",
        "session_speakers_set_updated_at",
        "session_users_set_updated_at",
    )
    _rename_index("session_speakers_speaker_id_idx", "session_users_user_id_idx")
    _rename_constraint(
        "session_speakers",
        "session_speakers_tenant_speaker_fkey",
        "session_users_tenant_user_fkey",
    )
    _rename_constraint(
        "session_speakers",
        "session_speakers_tenant_session_fkey",
        "session_users_tenant_session_fkey",
    )
    _rename_index_and_constraint(
        "session_speakers",
        "session_speakers_known_speaker_unique",
        "session_users_tenant_session_user_unique",
    )
    _rename_index_and_constraint(
        "session_speakers",
        "session_speakers_tenant_session_speaker_unique",
        "session_users_tenant_session_speaker_unique",
    )
    _rename_index_and_constraint(
        "session_speakers",
        "session_speakers_pkey",
        "session_users_pkey",
    )
    _rename_column("session_speakers", "speaker_id", "user_id")
    _rename_column("session_speakers", "session_speaker_id", "session_user_id")
    _rename_table("session_speakers", "session_users")

    _rename_trigger("speakers", "speakers_set_updated_at", "users_set_updated_at")
    _rename_index("speakers_external_ids_gin_idx", "users_external_ids_gin_idx")
    _rename_constraint(
        "speakers",
        "speakers_external_ids_object_check",
        "users_external_ids_object_check",
    )
    _rename_constraint("speakers", "speakers_tenant_id_fkey", "users_tenant_id_fkey")
    _rename_index_and_constraint(
        "speakers",
        "speakers_tenant_speaker_unique",
        "users_tenant_user_unique",
    )
    _rename_index_and_constraint("speakers", "speakers_pkey", "users_pkey")
    _rename_column("speakers", "speaker_id", "user_id")
    _rename_table("speakers", "users")


def _rename_table(old: str, new: str) -> None:
    op.rename_table(old, new, schema=SCHEMA)


def _rename_column(table: str, old: str, new: str) -> None:
    op.alter_column(table, old, new_column_name=new, schema=SCHEMA)


def _rename_constraint(table: str, old: str, new: str) -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.{table} RENAME CONSTRAINT {old} TO {new}")


def _rename_index(old: str, new: str) -> None:
    op.execute(f"ALTER INDEX {SCHEMA}.{old} RENAME TO {new}")


def _rename_index_and_constraint(_table: str, old: str, new: str) -> None:
    # A primary key or unique constraint shares its name with the backing index.
    # Renaming the index renames that constraint on this server.
    _rename_index(old, new)


def _rename_trigger(table: str, old: str, new: str) -> None:
    op.execute(f"ALTER TRIGGER {old} ON {SCHEMA}.{table} RENAME TO {new}")
