"""Integrity rules for the ceho_core mapped classes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from ceho.hot.outbox import insert_outbox_event
from ceho.models import (
    Channel,
    MediaObject,
    OutboxEvent,
    Session,
    SessionSpeaker,
    Speaker,
    Tenant,
    Transcript,
    User,
)
from tests.conftest import ActiveSession, as_table


def test_user_email_is_required_and_unique_per_tenant() -> None:
    users = as_table(User)
    email = _unique(users, "users_tenant_email_unique")

    assert not users.c.email.nullable
    assert [column.name for column in email.columns] == ["tenant_id", "email"]
    assert "users_email_check" in _check_names(users)
    assert "@" in str(_check(users, "users_email_check").sqltext)


def test_free_text_columns_reject_blanks_and_are_nullable_unless_required() -> None:
    required = {
        (Channel, "channel_name"),
        (Transcript, "text"),
        (MediaObject, "storage_uri"),
        (User, "email"),
    }
    optional = {
        (Tenant, "name"),
        (User, "display_name"),
        (Speaker, "display_name"),
        (Session, "session_description"),
        (MediaObject, "content_type"),
        (MediaObject, "codec"),
        (MediaObject, "checksum"),
    }
    for model, column_name in required | optional:
        table = as_table(model)
        column = table.c[column_name]
        assert column.nullable == ((model, column_name) not in required)
        assert f"{table.name}_{column_name}_check" in _check_names(table)


def test_storage_uri_is_unique() -> None:
    media = as_table(MediaObject)
    constraint = _unique(media, "media_objects_storage_uri_unique")

    assert [column.name for column in constraint.columns] == ["storage_uri"]


def test_transcript_speaker_cannot_be_deleted_while_referenced() -> None:
    foreign_key = _foreign_key(as_table(Transcript), "transcripts_speaker_fkey")

    assert foreign_key.ondelete == "NO ACTION"
    assert foreign_key.deferrable
    assert foreign_key.initially == "DEFERRED"


def test_pending_outbox_event_blocks_session_delete(active_session: ActiveSession) -> None:
    with active_session.connect() as connection:
        with connection.begin():
            insert_outbox_event(
                connection,
                tenant_id=active_session.tenant_id,
                session_id=active_session.session_id,
                event_type="session.created",
                sequence=0,
                payload={"hello": "session"},
            )
        sessions = as_table(Session)
        with pytest.raises(ProgrammingError, match="pending outbox event cannot be deleted"):
            with connection.begin():
                connection.execute(
                    sessions.delete().where(sessions.c.session_id == active_session.session_id)
                )


def test_published_outbox_event_is_deleted_with_its_session(
    active_session: ActiveSession,
) -> None:
    with active_session.connect() as connection:
        with connection.begin():
            event_id = insert_outbox_event(
                connection,
                tenant_id=active_session.tenant_id,
                session_id=active_session.session_id,
                event_type="session.ended",
                sequence=0,
                payload={"bye": "session"},
            )
            outbox = as_table(OutboxEvent)
            connection.execute(
                outbox.update()
                .where(outbox.c.event_id == event_id)
                .values(publication_status="published", published_at=text("now()"))
            )
            sessions = as_table(Session)
            connection.execute(
                sessions.delete().where(sessions.c.session_id == active_session.session_id)
            )
        remaining = connection.execute(
            text("SELECT count(*) FROM ceho_core.outbox_events WHERE event_id = :event_id"),
            {"event_id": event_id},
        ).scalar_one()

    assert remaining == 0


def test_session_delete_removes_its_speakers_and_transcripts(
    active_session: ActiveSession,
) -> None:
    with active_session.connect() as connection:
        with connection.begin():
            speakers = as_table(SessionSpeaker)
            speaker_id = connection.execute(
                speakers.insert()
                .values(tenant_id=active_session.tenant_id, session_id=active_session.session_id)
                .returning(speakers.c.session_speaker_id)
            ).scalar_one()
            connection.execute(
                as_table(Transcript)
                .insert()
                .values(
                    tenant_id=active_session.tenant_id,
                    session_id=active_session.session_id,
                    turn_index=0,
                    session_speaker_id=speaker_id,
                    start_at=active_session.started_at,
                    end_at=active_session.started_at,
                    text="hello",
                )
            )
            connection.execute(
                as_table(Session)
                .delete()
                .where(as_table(Session).c.session_id == active_session.session_id)
            )
        remaining = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM ceho_core.session_speakers
                     WHERE session_id = :session_id),
                    (SELECT count(*) FROM ceho_core.transcripts
                     WHERE session_id = :session_id)
                """
            ),
            {"session_id": active_session.session_id},
        ).one()

    assert remaining == (0, 0)


def test_session_speaker_with_a_transcript_cannot_be_deleted(
    active_session: ActiveSession,
) -> None:
    with active_session.connect() as connection:
        with connection.begin():
            speakers = as_table(SessionSpeaker)
            speaker_id = connection.execute(
                speakers.insert()
                .values(tenant_id=active_session.tenant_id, session_id=active_session.session_id)
                .returning(speakers.c.session_speaker_id)
            ).scalar_one()
            transcripts = as_table(Transcript)
            connection.execute(
                transcripts.insert().values(
                    tenant_id=active_session.tenant_id,
                    session_id=active_session.session_id,
                    turn_index=0,
                    session_speaker_id=speaker_id,
                    start_at=active_session.started_at,
                    end_at=active_session.started_at,
                    text="hello",
                )
            )
        with pytest.raises(IntegrityError, match="transcripts_speaker_fkey"):
            with connection.begin():
                connection.execute(
                    speakers.delete().where(speakers.c.session_speaker_id == speaker_id)
                )


def test_storage_uri_cannot_be_reused(active_session: ActiveSession) -> None:
    media = as_table(MediaObject)
    uri = f"s3://ceho/{uuid4()}"
    with active_session.connect() as connection:
        with connection.begin():
            connection.execute(
                media.insert().values(
                    tenant_id=active_session.tenant_id,
                    session_id=active_session.session_id,
                    storage_uri=uri,
                )
            )
        with pytest.raises(IntegrityError, match="media_objects_storage_uri_unique"):
            with connection.begin():
                connection.execute(
                    media.insert().values(
                        tenant_id=active_session.tenant_id,
                        session_id=active_session.session_id,
                        storage_uri=uri,
                    )
                )


def test_tenant_channel_cannot_reuse_a_generic_channel_name(
    active_session: ActiveSession,
) -> None:
    name = f"catalog-{uuid4()}"
    channels = as_table(Channel)
    with active_session.connect() as connection:
        with connection.begin():
            connection.execute(channels.insert().values(tenant_id=1, channel_name=name))
        try:
            with pytest.raises(ProgrammingError, match="shadows a generic channel"):
                with connection.begin():
                    connection.execute(
                        channels.insert().values(
                            tenant_id=active_session.tenant_id, channel_name=name
                        )
                    )
        finally:
            with connection.begin():
                connection.execute(
                    channels.delete().where(
                        channels.c.tenant_id == 1, channels.c.channel_name == name
                    )
                )


def test_generic_channel_cannot_reuse_a_tenant_channel_name(
    active_session: ActiveSession,
) -> None:
    channels = as_table(Channel)
    with active_session.connect() as connection:
        with connection.begin():
            name = connection.execute(
                text("SELECT channel_name FROM ceho_core.channels WHERE channel_id = :channel_id"),
                {"channel_id": active_session.channel_id},
            ).scalar_one()
        try:
            with pytest.raises(ProgrammingError, match="already used by a tenant"):
                with connection.begin():
                    connection.execute(channels.insert().values(tenant_id=1, channel_name=name))
        finally:
            with connection.begin():
                connection.execute(
                    channels.delete().where(
                        channels.c.tenant_id == 1, channels.c.channel_name == name
                    )
                )


def test_identity_sequences_are_named_after_their_table_and_column(
    active_session: ActiveSession,
) -> None:
    with active_session.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT cls.relname, attribute.attname, seq.relname
                FROM pg_class AS cls
                JOIN pg_namespace AS namespace ON namespace.oid = cls.relnamespace
                JOIN pg_attribute AS attribute ON attribute.attrelid = cls.oid
                JOIN pg_depend AS depend
                  ON depend.refobjid = cls.oid AND depend.refobjsubid = attribute.attnum
                JOIN pg_class AS seq ON seq.oid = depend.objid
                WHERE namespace.nspname = 'ceho_core'
                  AND attribute.attidentity <> ''
                  AND seq.relkind = 'S'
                """
            )
        ).all()

    assert rows
    assert {(table, column, sequence) for table, column, sequence in rows} == {
        (table, column, f"{table}_{column}_seq") for table, column, _sequence in rows
    }


def _foreign_key(table: Table, name: str) -> ForeignKeyConstraint:
    found = next(
        (
            constraint
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"{name} is missing")
    return found


def _unique(table: Table, name: str) -> UniqueConstraint:
    found = next(
        (
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name == name
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"{name} is missing")
    return found


def _check(table: Table, name: str) -> CheckConstraint:
    found = next(
        (
            constraint
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name == name
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"{name} is missing")
    return found


def _check_names(table: Table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str)
    }
