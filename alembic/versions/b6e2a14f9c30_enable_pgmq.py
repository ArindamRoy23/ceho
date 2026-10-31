"""enable pgmq

Revision ID: b6e2a14f9c30
Revises: d5e91b2c48aa
Create Date: 2026-10-31 17:01:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6e2a14f9c30"
down_revision: str | Sequence[str] | None = "d5e91b2c48aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable the pgmq extension. The devcontainer Postgres image ships it."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pgmq")


def downgrade() -> None:
    """Remove pgmq and the queues it owns."""
    op.execute("DROP EXTENSION IF EXISTS pgmq CASCADE")
