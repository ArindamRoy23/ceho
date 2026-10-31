"""Atomic final-transcript commits."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection

from ceho.hot.outbox import insert_outbox_event
from ceho.hot.session import (
    FinalizedTranscript,
    FinalizeTranscript,
    InvalidTranscript,
    MediaNotFound,
    SessionNotActive,
    SessionNotFound,
    SessionSpeakerNotFound,
    finalize_transcript,
)
from ceho.models import Base, MediaObject, OutboxEvent, Session, SessionSpeaker, Transcript
from tests.hot.conftest import ActiveSession, as_table


class _RejectingConnection:
    def begin(self) -> None:
        raise AssertionError("database used")


def _command(rows: ActiveSession, **overrides: object) -> FinalizeTranscript:
    values: dict[str, object] = {
        "tenant_id": rows.tenant_id,
        "session_id": rows.session_id,
        "text": "hello",
        "start_ms": 1_500,
        "end_ms": 2_700,
        "session_speaker_id": None,
        "label": None,
        "media_object_id": None,
        "media_start_offset_ms": None,
        "media_end_offset_ms": None,
    }
    values.update(overrides)
    return FinalizeTranscript(**values)  # type: ignore[arg-type]


def _count(connection: Connection, model: type[Base], session_id: int) -> int:
    relation = as_table(model)
    return int(
        connection.execute(
            select(func.count()).select_from(relation).where(relation.c.session_id == session_id)
        ).scalar_one()
    )


@pytest.mark.parametrize(
    ("text", "start_ms", "end_ms", "media_object_id", "label", "match"),
    [
        ("", 0, 1, None, None, "non-empty"),
        (" hello", 0, 1, None, None, "non-empty"),
        ("hello", 5, 4, None, None, "end_ms"),
        ("hello", 0, 1, 9, None, "media"),
        ("hello", 0, 1, None, " ", "label"),
    ],
)
def test_invalid_transcript_is_rejected_before_any_write(
    text: str,
    start_ms: int,
    end_ms: int,
    media_object_id: int | None,
    label: str | None,
    match: str,
) -> None:
    command = FinalizeTranscript(
        tenant_id=1,
        session_id=1,
        text=text,
        start_ms=start_ms,
        end_ms=end_ms,
        media_object_id=media_object_id,
        label=label,
    )

    with pytest.raises(InvalidTranscript, match=match):
        finalize_transcript(cast(Connection, _RejectingConnection()), command)


def test_missing_session_writes_nothing(active_session: ActiveSession) -> None:
    command = _command(active_session, session_id=active_session.session_id + 1_000_000)

    with active_session.connect() as connection:
        with pytest.raises(SessionNotFound, match="not found"):
            finalize_transcript(connection, command)
        assert _count(connection, Transcript, active_session.session_id) == 0
        assert _count(connection, OutboxEvent, active_session.session_id) == 0


def test_ended_session_writes_nothing(active_session: ActiveSession) -> None:
    with active_session.engine.begin() as connection:
        connection.execute(
            update(as_table(Session))
            .where(as_table(Session).c.session_id == active_session.session_id)
            .values(
                status="ended",
                ended_at=active_session.started_at + timedelta(seconds=1),
            )
        )

    with active_session.connect() as connection:
        with pytest.raises(SessionNotActive, match="not active"):
            finalize_transcript(connection, _command(active_session))
        assert _count(connection, Transcript, active_session.session_id) == 0


def test_committed_transcript_has_its_outbox_event(active_session: ActiveSession) -> None:
    with active_session.connect() as connection:
        finalized = finalize_transcript(connection, _command(active_session, label="speaker_1"))

    with active_session.connect() as connection:
        transcript = (
            connection.execute(
                select(as_table(Transcript)).where(
                    as_table(Transcript).c.session_id == active_session.session_id
                )
            )
            .mappings()
            .one()
        )
        event = (
            connection.execute(
                select(as_table(OutboxEvent)).where(
                    as_table(OutboxEvent).c.event_id == finalized.event_id
                )
            )
            .mappings()
            .one()
        )

    start_at = active_session.started_at + timedelta(milliseconds=1_500)
    end_at = active_session.started_at + timedelta(milliseconds=2_700)
    assert finalized.turn_index == 0
    assert finalized.sequence == 0
    assert transcript["turn_index"] == 0
    assert transcript["text"] == "hello"
    assert transcript["session_speaker_id"] == finalized.session_speaker_id
    assert transcript["start_at"] == start_at
    assert transcript["end_at"] == end_at
    assert event["event_type"] == "transcript.created"
    assert event["schema_version"] == 1
    assert event["sequence"] == 0
    assert event["publication_status"] == "pending"
    assert event["payload"]["turn_index"] == 0
    assert event["payload"]["text"] == "hello"
    assert event["payload"]["session_speaker_id"] == finalized.session_speaker_id
    assert event["payload"]["start_at"] == start_at.isoformat()
    assert event["payload"]["end_at"] == end_at.isoformat()


def test_turn_index_is_monotonic_for_a_session(active_session: ActiveSession) -> None:
    with active_session.connect() as connection:
        first = finalize_transcript(connection, _command(active_session, text="one"))
        second = finalize_transcript(
            connection,
            _command(active_session, text="two", session_speaker_id=first.session_speaker_id),
        )

    assert first.turn_index == 0
    assert second.turn_index == 1
    assert first.sequence == 0
    assert second.sequence == 1
    assert second.session_speaker_id == first.session_speaker_id


def test_outbox_insert_failure_rolls_back_the_transcript(
    active_session: ActiveSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_insert(*args: object, **kwargs: object) -> int:
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr("ceho.hot.session.insert_outbox_event", fail_insert)
    with active_session.connect() as connection:
        with pytest.raises(RuntimeError, match="outbox insert failed"):
            finalize_transcript(connection, _command(active_session))

    with active_session.connect() as connection:
        assert _count(connection, Transcript, active_session.session_id) == 0
        assert _count(connection, OutboxEvent, active_session.session_id) == 0
        assert _count(connection, SessionSpeaker, active_session.session_id) == 0


def test_first_final_creates_a_session_speaker_and_a_new_source_creates_another(
    active_session: ActiveSession,
) -> None:
    with active_session.connect() as connection:
        first = finalize_transcript(connection, _command(active_session, label="speaker_1"))
        reused = finalize_transcript(
            connection,
            _command(
                active_session,
                text="again",
                session_speaker_id=first.session_speaker_id,
            ),
        )
        created = finalize_transcript(connection, _command(active_session, text="other"))

    assert reused.session_speaker_id == first.session_speaker_id
    assert created.session_speaker_id != first.session_speaker_id
    with active_session.connect() as connection:
        labels = (
            connection.execute(
                select(as_table(SessionSpeaker).c.label).where(
                    as_table(SessionSpeaker).c.session_id == active_session.session_id
                )
            )
            .scalars()
            .all()
        )
    assert labels.count("speaker_1") == 1
    assert len(labels) == 2


def test_session_speaker_from_another_session_is_rejected(
    active_session: ActiveSession,
) -> None:
    with active_session.engine.begin() as connection:
        sessions = as_table(Session)
        other_session_id = connection.execute(
            sessions.insert()
            .values(
                tenant_id=active_session.tenant_id,
                channel_tenant_id=active_session.tenant_id,
                channel_id=active_session.channel_id,
                started_at=active_session.started_at,
            )
            .returning(sessions.c.session_id)
        ).scalar_one()
    with active_session.connect() as connection:
        finalized = finalize_transcript(connection, _command(active_session))
        with pytest.raises(SessionSpeakerNotFound, match="not found"):
            finalize_transcript(
                connection,
                _command(
                    active_session,
                    session_id=other_session_id,
                    session_speaker_id=finalized.session_speaker_id,
                ),
            )
        assert _count(connection, Transcript, other_session_id) == 0


def test_unknown_media_object_writes_nothing(active_session: ActiveSession) -> None:
    with active_session.engine.begin() as connection:
        sessions = as_table(Session)
        media = as_table(MediaObject)
        other_session_id = connection.execute(
            sessions.insert()
            .values(
                tenant_id=active_session.tenant_id,
                channel_tenant_id=active_session.tenant_id,
                channel_id=active_session.channel_id,
                started_at=active_session.started_at,
            )
            .returning(sessions.c.session_id)
        ).scalar_one()
        media_id = connection.execute(
            media.insert()
            .values(
                tenant_id=active_session.tenant_id,
                session_id=other_session_id,
                storage_uri="s3://ceho-media/other",
            )
            .returning(media.c.media_object_id)
        ).scalar_one()

    with active_session.connect() as connection:
        with pytest.raises(MediaNotFound, match="not found"):
            finalize_transcript(
                connection,
                _command(
                    active_session,
                    media_object_id=media_id,
                    media_start_offset_ms=0,
                    media_end_offset_ms=100,
                ),
            )
        assert _count(connection, Transcript, active_session.session_id) == 0
        assert _count(connection, SessionSpeaker, active_session.session_id) == 0


def test_media_provenance_is_stored_together(active_session: ActiveSession) -> None:
    with active_session.engine.begin() as connection:
        media = as_table(MediaObject)
        media_id = connection.execute(
            media.insert()
            .values(
                tenant_id=active_session.tenant_id,
                session_id=active_session.session_id,
                storage_uri="s3://ceho-media/source",
            )
            .returning(media.c.media_object_id)
        ).scalar_one()

    with active_session.connect() as connection:
        finalized = finalize_transcript(
            connection,
            _command(
                active_session,
                media_object_id=media_id,
                media_start_offset_ms=10,
                media_end_offset_ms=80,
            ),
        )
        transcript = (
            connection.execute(
                select(as_table(Transcript)).where(
                    as_table(Transcript).c.session_id == active_session.session_id,
                    as_table(Transcript).c.turn_index == finalized.turn_index,
                )
            )
            .mappings()
            .one()
        )

    assert transcript["media_object_id"] == media_id
    assert transcript["media_start_offset_ms"] == 10
    assert transcript["media_end_offset_ms"] == 80


def test_duplicate_session_speaker_label_is_rejected(active_session: ActiveSession) -> None:
    with active_session.connect() as connection:
        finalize_transcript(connection, _command(active_session, label="speaker_1"))
        with pytest.raises(InvalidTranscript, match="label"):
            finalize_transcript(
                connection, _command(active_session, text="other", label="speaker_1")
            )
        assert _count(connection, Transcript, active_session.session_id) == 1


def test_outbox_sequence_stays_independent_of_turn_index(
    active_session: ActiveSession,
) -> None:
    with active_session.connect() as connection:
        with connection.begin():
            insert_outbox_event(
                connection,
                tenant_id=active_session.tenant_id,
                session_id=active_session.session_id,
                event_type="session.created",
                sequence=0,
                payload={"source": "test"},
            )
        finalized = finalize_transcript(connection, _command(active_session))

    assert finalized.turn_index == 0
    assert finalized.sequence == 1


def test_concurrent_finalizes_allocate_distinct_turn_indexes(
    active_session: ActiveSession,
) -> None:
    barrier = threading.Barrier(2)
    results: list[FinalizedTranscript] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def work() -> None:
        try:
            barrier.wait(timeout=5)
            with active_session.connect() as connection:
                finalized = finalize_transcript(connection, _command(active_session))
            with lock:
                results.append(finalized)
        except BaseException as error:  # pragma: no cover - assertion below reports it
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=work), threading.Thread(target=work)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(result.turn_index for result in results) == [0, 1]
    assert sorted(result.sequence for result in results) == [0, 1]
    assert len({result.session_speaker_id for result in results}) == 2
