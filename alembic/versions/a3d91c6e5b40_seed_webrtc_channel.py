"""seed the WebRTC catalog channel

Revision ID: a3d91c6e5b40
Revises: f9a656e37280
Create Date: 2026-11-27 10:47:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d91c6e5b40"
down_revision: str | Sequence[str] | None = "f9a656e37280"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches ceho.models.GENERIC_TENANT_ID. Frozen here so this revision stays stable.
_GENERIC_TENANT_ID = 1


def upgrade() -> None:
    """Insert the generic WebRTC catalog row when that name is missing.

    Tenant 1 already exists. A new database has no channels, so the identity
    assigns channel_id 1. Other channel rows are left in place.
    """
    op.execute(
        f"""
        INSERT INTO ceho_core.channels (tenant_id, channel_name)
        SELECT {_GENERIC_TENANT_ID}, 'WebRTC'
        WHERE NOT EXISTS (
            SELECT 1 FROM ceho_core.channels
            WHERE tenant_id = {_GENERIC_TENANT_ID} AND channel_name = 'WebRTC'
        )
        """
    )


def downgrade() -> None:
    """Remove the seeded WebRTC row when no session uses it."""
    op.execute(
        f"""
        DELETE FROM ceho_core.channels AS channel
        WHERE channel.tenant_id = {_GENERIC_TENANT_ID}
          AND channel.channel_name = 'WebRTC'
          AND NOT EXISTS (
              SELECT 1 FROM ceho_core.sessions AS session
              WHERE session.channel_tenant_id = channel.tenant_id
                AND session.channel_id = channel.channel_id
          )
        """
    )
