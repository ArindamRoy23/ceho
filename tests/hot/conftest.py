"""PostgreSQL fixtures for hot-path durability tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from sqlalchemy import Engine, Table, text
from sqlalchemy.engine import Connection

from ceho.hot.session import create_database_engine
from ceho.models import Base, Channel, Session, Tenant


def as_table(model: type[Base]) -> Table:
    """Return the mapped table. Mypy otherwise sees a generic FROM clause."""
    relation = model.__table__
    if not isinstance(relation, Table):
        raise TypeError(model.__name__)
    return relation


@dataclass
class ActiveSession:
    """One committed active session, cleaned up with its tenant."""

    engine: Engine
    tenant_id: int
    channel_id: int
    session_id: int
    started_at: datetime

    def connect(self) -> Connection:
        """Open a connection. The caller closes it."""
        return self.engine.connect()


def _engine() -> Engine:
    load_dotenv()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pytest.skip("DATABASE_URL is not set")
    engine = create_database_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        engine.dispose()
        pytest.skip(f"PostgreSQL is not reachable: {error}")
    return engine


@pytest.fixture
def active_session() -> Iterator[ActiveSession]:
    """Insert a tenant, channel, and active session, then delete them."""
    engine = _engine()
    started_at = datetime.fromisoformat("2026-10-31T09:00:00+00:00")
    with engine.begin() as connection:
        tenants = as_table(Tenant)
        channels = as_table(Channel)
        sessions = as_table(Session)
        tenant_id = connection.execute(
            tenants.insert().values(name=f"finalize-{uuid4()}").returning(tenants.c.tenant_id)
        ).scalar_one()
        channel_id = connection.execute(
            channels.insert()
            .values(tenant_id=tenant_id, channel_name=f"direct-{uuid4()}")
            .returning(channels.c.channel_id)
        ).scalar_one()
        session_id = connection.execute(
            sessions.insert()
            .values(
                tenant_id=tenant_id,
                channel_tenant_id=tenant_id,
                channel_id=channel_id,
                started_at=started_at,
            )
            .returning(sessions.c.session_id)
        ).scalar_one()
    rows = ActiveSession(
        engine=engine,
        tenant_id=tenant_id,
        channel_id=channel_id,
        session_id=session_id,
        started_at=started_at,
    )
    try:
        yield rows
    finally:
        with engine.begin() as connection:
            connection.execute(
                as_table(Session).delete().where(as_table(Session).c.tenant_id == tenant_id)
            )
            connection.execute(
                as_table(Channel).delete().where(as_table(Channel).c.tenant_id == tenant_id)
            )
            connection.execute(
                as_table(Tenant).delete().where(as_table(Tenant).c.tenant_id == tenant_id)
            )
        engine.dispose()
