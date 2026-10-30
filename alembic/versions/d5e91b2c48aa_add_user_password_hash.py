"""add user password hash

Revision ID: d5e91b2c48aa
Revises: c4d8f0a91b27
Create Date: 2026-10-30 15:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e91b2c48aa"
down_revision: str | Sequence[str] | None = "c4d8f0a91b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store a local password as an Argon2id hash."""
    op.add_column(
        "users",
        sa.Column("password_hash", sa.Text(), nullable=True),
        schema="ceho_core",
    )
    op.create_check_constraint(
        "users_password_hash_check",
        "users",
        "password_hash IS NULL OR password_hash LIKE '$argon2id$%'",
        schema="ceho_core",
    )


def downgrade() -> None:
    """Remove the local password hash from platform users."""
    op.drop_constraint(
        "users_password_hash_check",
        "users",
        schema="ceho_core",
        type_="check",
    )
    op.drop_column("users", "password_hash", schema="ceho_core")
