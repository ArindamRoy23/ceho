"""Session control against PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine

from ceho.hot.outbox import EventEnvelope
from ceho.hot.session import (
    ControlError,
    SqlControlStore,
    create_session,
    end_session,
    join_session,
)
from ceho.models import Channel, OutboxEvent, Session, SessionSpeaker, Tenant
from tests.conftest import as_table, database_engine

NOW = datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc)


class _Publisher:
    def __init__(self) -> None:
        self.event_types: list[str] = []

    @property
    def preserves_session_order(self) -> bool:
        return True

    def publish(self, envelope: EventEnvelope) -> None:
        self.event_types.append(envelope.event_type)


def _cleanup(engine: Engine, tenant_id: int) -> None:
    with engine.begin() as connection:
        outbox = as_table(OutboxEvent)
        connection.execute(
            update(outbox)
            .where(
                outbox.c.tenant_id == tenant_id,
                outbox.c.publication_status == "pending",
            )
            .values(publication_status="failed")
        )
        connection.execute(
            as_table(Session).delete().where(as_table(Session).c.tenant_id == tenant_id)
        )
        connection.execute(
            as_table(Channel).delete().where(as_table(Channel).c.tenant_id == tenant_id)
        )
        connection.execute(
            as_table(Tenant).delete().where(as_table(Tenant).c.tenant_id == tenant_id)
        )


def test_create_join_and_end_commit_one_session_and_two_events() -> None:
    engine = database_engine()
    tenant_id = 0
    try:
        with engine.begin() as connection:
            tenants = as_table(Tenant)
            channels = as_table(Channel)
            tenant_id = int(
                connection.execute(
                    tenants.insert()
                    .values(name=f"control-{uuid4()}")
                    .returning(tenants.c.tenant_id)
                ).scalar_one()
            )
            channel_id = int(
                connection.execute(
                    channels.insert()
                    .values(tenant_id=tenant_id, channel_name=f"direct-{uuid4()}")
                    .returning(channels.c.channel_id)
                ).scalar_one()
            )
        publisher = _Publisher()
        with engine.connect() as connection:
            store = SqlControlStore(connection)
            issued = create_session(
                store,
                tenant_id=tenant_id,
                user_id=7,
                channel_id=channel_id,
                description="planning",
                now=NOW,
                secret="signaling-secret",
                ttl_seconds=60,
                publisher=publisher,
            )
            joined = join_session(
                store,
                tenant_id=tenant_id,
                user_id=7,
                session_id=issued.session.session_id,
                now=NOW + timedelta(minutes=1),
                secret="signaling-secret",
                ttl_seconds=60,
            )
            ended = end_session(
                store,
                tenant_id=tenant_id,
                user_id=7,
                session_id=issued.session.session_id,
                now=NOW + timedelta(minutes=2),
                reason="explicit",
                publisher=publisher,
            )
            with pytest.raises(ControlError, match="session has ended"):
                join_session(
                    store,
                    tenant_id=tenant_id,
                    user_id=7,
                    session_id=issued.session.session_id,
                    now=NOW + timedelta(minutes=3),
                    secret="signaling-secret",
                    ttl_seconds=60,
                )
        with engine.connect() as connection:
            sessions = as_table(Session)
            speakers = as_table(SessionSpeaker)
            outbox = as_table(OutboxEvent)
            session_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(sessions)
                    .where(sessions.c.tenant_id == tenant_id)
                ).scalar_one()
            )
            speaker_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(speakers)
                    .where(speakers.c.tenant_id == tenant_id)
                ).scalar_one()
            )
            events = connection.execute(
                select(outbox.c.event_type, outbox.c.sequence, outbox.c.publication_status)
                .where(outbox.c.tenant_id == tenant_id)
                .order_by(outbox.c.sequence)
            ).all()
            status = connection.execute(
                select(sessions.c.status).where(sessions.c.session_id == issued.session.session_id)
            ).scalar_one()
        assert joined.session.session_id == issued.session.session_id
        assert ended.status == "ended"
        assert session_count == 1
        assert speaker_count == 0
        assert status == "ended"
        assert [(row.event_type, row.sequence) for row in events] == [
            ("session.created", 0),
            ("session.ended", 1),
        ]
        assert {row.publication_status for row in events} == {"published"}
        assert publisher.event_types == ["session.created", "session.ended"]
    finally:
        if tenant_id:
            _cleanup(engine, tenant_id)
        engine.dispose()


def test_a_channel_from_another_tenant_writes_nothing() -> None:
    engine = database_engine()
    owner_id = 0
    other_id = 0
    try:
        with engine.begin() as connection:
            tenants = as_table(Tenant)
            channels = as_table(Channel)
            owner_id = int(
                connection.execute(
                    tenants.insert().values(name=f"owner-{uuid4()}").returning(tenants.c.tenant_id)
                ).scalar_one()
            )
            other_id = int(
                connection.execute(
                    tenants.insert().values(name=f"other-{uuid4()}").returning(tenants.c.tenant_id)
                ).scalar_one()
            )
            channel_id = int(
                connection.execute(
                    channels.insert()
                    .values(tenant_id=other_id, channel_name=f"private-{uuid4()}")
                    .returning(channels.c.channel_id)
                ).scalar_one()
            )
        with engine.connect() as connection:
            with pytest.raises(ControlError, match="not available") as caught:
                create_session(
                    SqlControlStore(connection),
                    tenant_id=owner_id,
                    user_id=7,
                    channel_id=channel_id,
                    description=None,
                    now=NOW,
                    secret="signaling-secret",
                    ttl_seconds=60,
                )
        assert caught.value.status_code == 403
        with engine.connect() as connection:
            sessions = as_table(Session)
            count = int(
                connection.execute(
                    select(func.count())
                    .select_from(sessions)
                    .where(sessions.c.tenant_id == owner_id)
                ).scalar_one()
            )
        assert count == 0
    finally:
        if owner_id:
            _cleanup(engine, owner_id)
        if other_id:
            _cleanup(engine, other_id)
        engine.dispose()
