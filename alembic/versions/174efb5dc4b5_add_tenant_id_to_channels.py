"""add tenant_id to channels

Revision ID: 174efb5dc4b5
Revises: c05bbb7471b6
Create Date: 2026-10-27 08:53:54.430295

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "174efb5dc4b5"
down_revision: str | Sequence[str] | None = "c05bbb7471b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches ceho.models.GENERIC_TENANT_ID. Frozen here so this revision stays stable.
_GENERIC_TENANT_ID = 1


def upgrade() -> None:
    """Upgrade schema."""
    # Tenant 1 is the generic tenant. Existing channels had no owner, so they
    # become that catalog, and existing sessions keep pointing at them.
    op.execute(
        f"""
        INSERT INTO ceho_core.tenants (tenant_id, name)
        OVERRIDING SYSTEM VALUE
        VALUES ({_GENERIC_TENANT_ID}, 'generic')
        ON CONFLICT (tenant_id) DO NOTHING
        """
    )
    op.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence('ceho_core.tenants', 'tenant_id'),
            GREATEST((SELECT MAX(tenant_id) FROM ceho_core.tenants), {_GENERIC_TENANT_ID})
        )
        """
    )
    op.add_column(
        "channels",
        sa.Column("tenant_id", sa.BigInteger(), nullable=True),
        schema="ceho_core",
    )
    op.execute(f"UPDATE ceho_core.channels SET tenant_id = {_GENERIC_TENANT_ID}")
    op.alter_column(
        "channels",
        "tenant_id",
        existing_type=sa.BigInteger(),
        nullable=False,
        schema="ceho_core",
    )
    op.create_foreign_key(
        "channels_tenant_id_fkey",
        "channels",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        source_schema="ceho_core",
        referent_schema="ceho_core",
    )
    op.drop_constraint(
        "channels_channel_name_key",
        "channels",
        schema="ceho_core",
        type_="unique",
    )
    op.create_unique_constraint(
        "channels_tenant_channel_unique",
        "channels",
        ["tenant_id", "channel_id"],
        schema="ceho_core",
    )
    op.create_unique_constraint(
        "channels_tenant_name_unique",
        "channels",
        ["tenant_id", "channel_name"],
        schema="ceho_core",
    )
    op.add_column(
        "sessions",
        sa.Column("channel_tenant_id", sa.BigInteger(), nullable=True),
        schema="ceho_core",
    )
    op.execute(f"UPDATE ceho_core.sessions SET channel_tenant_id = {_GENERIC_TENANT_ID}")
    op.alter_column(
        "sessions",
        "channel_tenant_id",
        existing_type=sa.BigInteger(),
        nullable=False,
        schema="ceho_core",
    )
    op.drop_index("sessions_channel_id_idx", table_name="sessions", schema="ceho_core")
    op.drop_constraint(
        "sessions_channel_id_fkey",
        "sessions",
        schema="ceho_core",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "sessions_tenant_channel_fkey",
        "sessions",
        "channels",
        ["channel_tenant_id", "channel_id"],
        ["tenant_id", "channel_id"],
        source_schema="ceho_core",
        referent_schema="ceho_core",
    )
    op.create_check_constraint(
        "sessions_channel_tenant_check",
        "sessions",
        f"channel_tenant_id = tenant_id OR channel_tenant_id = {_GENERIC_TENANT_ID}",
        schema="ceho_core",
    )
    op.create_index(
        "sessions_channel_idx",
        "sessions",
        ["channel_tenant_id", "channel_id"],
        unique=False,
        schema="ceho_core",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("sessions_channel_idx", table_name="sessions", schema="ceho_core")
    op.drop_constraint(
        "sessions_channel_tenant_check",
        "sessions",
        schema="ceho_core",
        type_="check",
    )
    op.drop_constraint(
        "sessions_tenant_channel_fkey",
        "sessions",
        schema="ceho_core",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "sessions_channel_id_fkey",
        "sessions",
        "channels",
        ["channel_id"],
        ["channel_id"],
        source_schema="ceho_core",
        referent_schema="ceho_core",
    )
    op.create_index(
        "sessions_channel_id_idx",
        "sessions",
        ["channel_id"],
        unique=False,
        schema="ceho_core",
    )
    op.drop_column("sessions", "channel_tenant_id", schema="ceho_core")
    op.drop_constraint(
        "channels_tenant_name_unique",
        "channels",
        schema="ceho_core",
        type_="unique",
    )
    op.drop_constraint(
        "channels_tenant_channel_unique",
        "channels",
        schema="ceho_core",
        type_="unique",
    )
    op.create_unique_constraint(
        "channels_channel_name_key",
        "channels",
        ["channel_name"],
        schema="ceho_core",
    )
    op.drop_constraint(
        "channels_tenant_id_fkey",
        "channels",
        schema="ceho_core",
        type_="foreignkey",
    )
    op.drop_column("channels", "tenant_id", schema="ceho_core")
    op.execute(f"DELETE FROM ceho_core.tenants WHERE tenant_id = {_GENERIC_TENANT_ID}")
    op.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('ceho_core.tenants', 'tenant_id'),
            COALESCE((SELECT MAX(tenant_id) FROM ceho_core.tenants), 1),
            (SELECT MAX(tenant_id) FROM ceho_core.tenants) IS NOT NULL
        )
        """
    )
