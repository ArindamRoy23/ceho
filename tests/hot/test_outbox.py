"""Outbox envelope, publisher, and at-least-once delivery."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ceho.hot.config import ConfigurationError
from ceho.hot.outbox import (
    SCHEMA_VERSION,
    DatabaseOutboxStore,
    EventEnvelope,
    MemoryEventPublisher,
    insert_outbox_event,
    publish_pending,
    publisher_from_url,
    transcript_created_payload,
)
from ceho.hot.session import FinalizeTranscript, finalize_transcript
from ceho.models import OutboxEvent
from tests.hot.conftest import ActiveSession

OCCURRED_AT = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


def _envelope(
    *,
    event_id: int,
    session_id: int,
    sequence: int,
    tenant_id: int = 1,
    event_type: str = "transcript.created",
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        schema_version=SCHEMA_VERSION,
        event_type=event_type,
        tenant_id=tenant_id,
        session_id=session_id,
        sequence=sequence,
        occurred_at=OCCURRED_AT,
        payload={"turn_index": sequence, "text": "hello"},
    )


class _Row:
    def __init__(self, envelope: EventEnvelope) -> None:
        self.envelope = envelope
        self.status = "pending"
        self.attempt_count = 0


class RecordingOutbox:
    """In-memory outbox with the same claim rule as the database store."""

    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows
        self.claims = 0

    def claim_next(self, blocked_sessions: Collection[tuple[int, int]]) -> EventEnvelope | None:
        self.claims += 1
        if self.claims > 10:
            raise AssertionError("publish loop did not stop retrying a pending event")
        blocked = set(blocked_sessions)
        eligible: list[_Row] = []
        for row in self.rows:
            envelope = row.envelope
            if row.status != "pending":
                continue
            if (envelope.tenant_id, envelope.session_id) in blocked:
                continue
            earlier_unpublished = any(
                other.envelope.tenant_id == envelope.tenant_id
                and other.envelope.session_id == envelope.session_id
                and other.envelope.sequence < envelope.sequence
                and other.status != "published"
                for other in self.rows
            )
            if not earlier_unpublished:
                eligible.append(row)
        if not eligible:
            return None
        chosen = min(
            eligible,
            key=lambda row: (
                row.envelope.tenant_id,
                row.envelope.session_id,
                row.envelope.sequence,
            ),
        )
        chosen.attempt_count += 1
        return chosen.envelope

    def mark_published(self, event_id: int) -> None:
        for row in self.rows:
            if row.envelope.event_id == event_id and row.status == "pending":
                row.status = "published"
                return
        raise AssertionError(f"event {event_id} was not pending")


class _Publisher:
    def __init__(self, fail_sessions: set[int] | None = None) -> None:
        self.fail_sessions = fail_sessions or set()
        self.sent: list[tuple[int, int, int]] = []

    @property
    def preserves_session_order(self) -> bool:
        return True

    def publish(self, envelope: EventEnvelope) -> None:
        self.sent.append((envelope.session_id, envelope.sequence, envelope.event_id))
        if envelope.session_id in self.fail_sessions:
            raise RuntimeError("bus down")


def test_transcript_created_payload_is_version_1() -> None:
    start_at = OCCURRED_AT
    end_at = OCCURRED_AT + timedelta(milliseconds=400)
    payload = transcript_created_payload(
        turn_index=3,
        session_speaker_id=8,
        text="hello",
        start_at=start_at,
        end_at=end_at,
        media_object_id=None,
        media_start_offset_ms=None,
        media_end_offset_ms=None,
    )

    assert SCHEMA_VERSION == 1
    assert payload == {
        "turn_index": 3,
        "session_speaker_id": 8,
        "text": "hello",
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "media_object_id": None,
        "media_start_offset_ms": None,
        "media_end_offset_ms": None,
    }


def test_memory_publisher_keeps_events_in_call_order() -> None:
    publisher = publisher_from_url("memory://ceho")
    assert isinstance(publisher, MemoryEventPublisher)
    assert publisher.preserves_session_order is True
    first = _envelope(event_id=1, session_id=4, sequence=0)
    second = _envelope(event_id=2, session_id=4, sequence=1)

    publisher.publish(first)
    publisher.publish(second)

    assert publisher.events == [first, second]


def test_unknown_publisher_scheme_fails_clearly() -> None:
    with pytest.raises(ConfigurationError, match="CEHO_EVENT_PUBLISHER_URL"):
        publisher_from_url("kafka://broker:9092")


def test_publish_failure_blocks_later_sequences_and_continues_other_sessions() -> None:
    rows = [
        _Row(_envelope(event_id=10, session_id=1, sequence=0)),
        _Row(_envelope(event_id=11, session_id=1, sequence=1)),
        _Row(_envelope(event_id=20, session_id=2, sequence=0)),
    ]
    publisher = _Publisher(fail_sessions={1})

    published = publish_pending(RecordingOutbox(rows), publisher)

    assert published == 1
    assert (1, 1, 11) not in publisher.sent
    assert (2, 0, 20) in publisher.sent
    assert rows[0].status == "pending"
    assert rows[1].status == "pending"
    assert rows[2].status == "published"


def test_crash_before_send_retries_the_pending_event() -> None:
    row = _Row(_envelope(event_id=10, session_id=1, sequence=0))
    store = RecordingOutbox([row])
    failing = _Publisher(fail_sessions={1})

    assert publish_pending(store, failing) == 0
    assert row.status == "pending"
    assert row.attempt_count == 1

    delivered = MemoryEventPublisher()
    assert publish_pending(store, delivered) == 1
    assert [event.event_id for event in delivered.events] == [10]
    assert row.status == "published"


def test_crash_after_send_redelivers_the_same_event_id() -> None:
    row = _Row(_envelope(event_id=10, session_id=1, sequence=0))
    store = RecordingOutbox([row])

    class CrashAfterSend:
        def __init__(self) -> None:
            self.event_ids: list[int] = []
            self.crash = True

        @property
        def preserves_session_order(self) -> bool:
            return True

        def publish(self, envelope: EventEnvelope) -> None:
            self.event_ids.append(envelope.event_id)
            if self.crash:
                self.crash = False
                raise RuntimeError("crash after send")

    publisher = CrashAfterSend()
    assert publish_pending(store, publisher) == 0
    assert row.status == "pending"

    assert publish_pending(store, publisher) == 1
    assert publisher.event_ids == [10, 10]
    assert row.status == "published"


def test_events_publish_in_session_sequence_order() -> None:
    rows = [
        _Row(_envelope(event_id=2, session_id=1, sequence=1)),
        _Row(_envelope(event_id=1, session_id=1, sequence=0)),
    ]
    publisher = MemoryEventPublisher()

    assert publish_pending(RecordingOutbox(rows), publisher) == 2
    assert [event.event_id for event in publisher.events] == [1, 2]


def _command(rows: ActiveSession) -> FinalizeTranscript:
    return FinalizeTranscript(
        tenant_id=rows.tenant_id,
        session_id=rows.session_id,
        text="hello",
        start_ms=500,
        end_ms=900,
    )


def test_committed_transcript_is_visible_before_the_bus_accepts_it(
    active_session: ActiveSession,
) -> None:
    class SeeCommit:
        def __init__(self) -> None:
            self.saw_transcript = False

        @property
        def preserves_session_order(self) -> bool:
            return True

        def publish(self, envelope: EventEnvelope) -> None:
            with active_session.engine.connect() as connection:
                transcript = connection.execute(
                    select(OutboxEvent.__table__.c.publication_status).where(
                        OutboxEvent.__table__.c.event_id == envelope.event_id
                    )
                ).scalar_one()
            self.saw_transcript = transcript == "pending"
            raise RuntimeError("bus down")

    publisher = SeeCommit()
    with active_session.connect() as connection:
        finalized = finalize_transcript(connection, _command(active_session), publisher)

    assert publisher.saw_transcript is True
    with active_session.connect() as connection:
        status = connection.execute(
            select(OutboxEvent.__table__.c.publication_status).where(
                OutboxEvent.__table__.c.event_id == finalized.event_id
            )
        ).scalar_one()
    assert status == "pending"


def test_database_redelivers_the_same_event_id_after_a_crash(
    active_session: ActiveSession,
) -> None:
    with active_session.connect() as connection:
        finalized = finalize_transcript(connection, _command(active_session))

    class CrashAfterSend:
        def __init__(self) -> None:
            self.event_ids: list[int] = []
            self.crash = True

        @property
        def preserves_session_order(self) -> bool:
            return True

        def publish(self, envelope: EventEnvelope) -> None:
            self.event_ids.append(envelope.event_id)
            if self.crash:
                self.crash = False
                raise RuntimeError("crash after send")

    publisher = CrashAfterSend()
    with active_session.connect() as connection:
        assert (
            publish_pending(
                DatabaseOutboxStore(
                    connection,
                    tenant_id=active_session.tenant_id,
                    session_id=active_session.session_id,
                ),
                publisher,
            )
            == 0
        )
    with active_session.connect() as connection:
        status = connection.execute(
            select(OutboxEvent.__table__.c.publication_status).where(
                OutboxEvent.__table__.c.event_id == finalized.event_id
            )
        ).scalar_one()
    assert status == "pending"

    with active_session.connect() as connection:
        assert (
            publish_pending(
                DatabaseOutboxStore(
                    connection,
                    tenant_id=active_session.tenant_id,
                    session_id=active_session.session_id,
                ),
                publisher,
            )
            == 1
        )
    assert publisher.event_ids == [finalized.event_id, finalized.event_id]


def test_database_publishes_an_earlier_session_event_before_the_transcript(
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
                payload={"hello": "session"},
            )
        finalized = finalize_transcript(connection, _command(active_session))
    assert finalized.turn_index == 0
    assert finalized.sequence == 1

    publisher = MemoryEventPublisher()
    with active_session.connect() as connection:
        assert (
            publish_pending(
                DatabaseOutboxStore(
                    connection,
                    tenant_id=active_session.tenant_id,
                    session_id=active_session.session_id,
                ),
                publisher,
            )
            == 2
        )

    assert [(event.event_type, event.sequence) for event in publisher.events] == [
        ("session.created", 0),
        ("transcript.created", 1),
    ]
    assert publisher.events[1].event_id == finalized.event_id
