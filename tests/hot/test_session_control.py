"""Tests for session create, join, end, and idle termination."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ceho.hot.credentials import authorize_signaling, signaling_grant
from ceho.hot.session import (
    ControlError,
    IssuedSession,
    PresenceBook,
    create_session,
    end_idle_sessions,
    end_session,
    join_session,
)
from tests.hot.conftest import MemoryControlStore
from tests.hot.test_credentials import NOW, SECRET, TTL


def _store() -> MemoryControlStore:
    store = MemoryControlStore()
    store.allow(4, 2)
    return store


def _open(
    store: MemoryControlStore,
    *,
    when: datetime = NOW,
    channel_id: int = 2,
    description: str | None = None,
) -> IssuedSession:
    return create_session(
        store,
        tenant_id=4,
        channel_id=channel_id,
        description=description,
        now=when,
        secret=SECRET,
        ttl=TTL,
    )


def test_create_writes_the_session_and_outbox_together() -> None:
    store = _store()

    issued = _open(store, description="  standup  ")

    assert issued.session.status == "active"
    assert issued.session.tenant_id == 4
    assert issued.session.description == "standup"
    assert len(store.sessions) == 1
    assert len(store.outbox) == 1
    event = store.outbox[0]
    assert event.event_type == "session.created"
    assert event.sequence == 0
    assert event.publication_status == "pending"
    assert event.schema_version == 1
    assert event.payload["started_at"] == NOW.isoformat()
    assert "user_id" not in event.payload
    assert "speaker_id" not in event.payload
    assert store.speakers == []
    grant = signaling_grant(issued.signaling_token, SECRET, NOW)
    authorize_signaling(
        grant,
        tenant_id=4,
        session_id=issued.session.session_id,
        channel_tenant_id=4,
        channel_id=2,
    )


def test_create_rolls_back_when_the_outbox_write_fails() -> None:
    store = _store()
    store.fail_on_outbox = True

    with pytest.raises(RuntimeError, match="outbox failed"):
        _open(store)

    assert store.sessions == {}
    assert store.outbox == []


def test_create_rolls_back_when_commit_fails() -> None:
    store = _store()
    store.fail_commit = True

    with pytest.raises(RuntimeError, match="commit failed"):
        _open(store)

    assert store.sessions == {}
    assert store.outbox == []


def test_missing_channel_is_rejected() -> None:
    store = _store()

    with pytest.raises(ControlError, match="channel does not exist") as caught:
        _open(store, channel_id=9)

    assert caught.value.status_code == 404
    assert store.sessions == {}


def test_channel_from_another_tenant_is_rejected() -> None:
    store = _store()
    store.channels[8] = (9, 8)

    with pytest.raises(ControlError, match="not available") as caught:
        _open(store, channel_id=8)

    assert caught.value.status_code == 403
    assert store.sessions == {}


def test_generic_tenant_channel_is_allowed() -> None:
    store = _store()
    store.channels[3] = (1, 3)

    issued = _open(store, channel_id=3)

    assert issued.session.tenant_id == 4
    assert issued.session.channel_tenant_id == 1
    assert issued.session.channel_id == 3


def test_missing_tenant_is_rejected() -> None:
    store = MemoryControlStore()
    store.channels[2] = (4, 2)

    with pytest.raises(ControlError, match="tenant does not exist") as caught:
        _open(store)

    assert caught.value.status_code == 401


def test_join_reuses_the_session_and_writes_no_speaker() -> None:
    store = _store()
    created = _open(store)

    joined = join_session(
        store,
        tenant_id=4,
        session_id=created.session.session_id,
        now=NOW + timedelta(minutes=5),
        secret=SECRET,
        ttl=TTL,
    )

    assert joined.session.session_id == created.session.session_id
    assert len(store.sessions) == 1
    assert [event.event_type for event in store.outbox] == ["session.created"]
    assert store.speakers == []


def test_join_rejects_a_session_from_another_tenant() -> None:
    store = _store()
    created = _open(store)
    store.allow(5, 7)

    with pytest.raises(ControlError, match="session does not exist") as caught:
        join_session(
            store,
            tenant_id=5,
            session_id=created.session.session_id,
            now=NOW,
            secret=SECRET,
            ttl=TTL,
        )

    assert caught.value.status_code == 404


def test_ended_session_rejects_a_new_join() -> None:
    store = _store()
    created = _open(store)
    end_session(
        store,
        tenant_id=4,
        session_id=created.session.session_id,
        now=NOW + timedelta(minutes=1),
        reason="explicit",
    )

    with pytest.raises(ControlError, match="session has ended") as caught:
        join_session(
            store,
            tenant_id=4,
            session_id=created.session.session_id,
            now=NOW + timedelta(minutes=2),
            secret=SECRET,
            ttl=TTL,
        )

    assert caught.value.status_code == 409


def test_end_writes_one_ended_event() -> None:
    store = _store()
    created = _open(store)
    ended_at = NOW + timedelta(minutes=1)

    ended = end_session(
        store,
        tenant_id=4,
        session_id=created.session.session_id,
        now=ended_at,
        reason="explicit",
    )

    assert ended.status == "ended"
    assert ended.ended_at == ended_at
    ended_events = [event for event in store.outbox if event.event_type == "session.ended"]
    assert len(ended_events) == 1
    assert ended_events[0].sequence == 1
    assert ended_events[0].payload["reason"] == "explicit"
    assert ended_events[0].publication_status == "pending"


def test_second_end_does_not_write_another_event() -> None:
    store = _store()
    created = _open(store)
    end_session(
        store,
        tenant_id=4,
        session_id=created.session.session_id,
        now=NOW + timedelta(minutes=1),
        reason="explicit",
    )

    with pytest.raises(ControlError, match="already ended") as caught:
        end_session(
            store,
            tenant_id=4,
            session_id=created.session.session_id,
            now=NOW + timedelta(minutes=2),
            reason="explicit",
        )

    assert caught.value.status_code == 409
    assert [event.event_type for event in store.outbox] == ["session.created", "session.ended"]


def test_end_rolls_back_when_commit_fails() -> None:
    store = _store()
    created = _open(store)
    store.fail_commit = True

    with pytest.raises(RuntimeError, match="commit failed"):
        end_session(
            store,
            tenant_id=4,
            session_id=created.session.session_id,
            now=NOW + timedelta(minutes=1),
            reason="explicit",
        )

    assert store.sessions[(4, created.session.session_id)].status == "active"
    assert [event.event_type for event in store.outbox] == ["session.created"]


def test_idle_ends_a_quiet_session() -> None:
    store = _store()
    created = _open(store, when=NOW - timedelta(hours=2))

    ended = end_idle_sessions(store, PresenceBook(), idle_seconds=900, now=NOW)

    assert ended == [created.session.session_id]
    reasons = [
        event.payload["reason"] for event in store.outbox if event.event_type == "session.ended"
    ]
    assert reasons == ["idle"]


def test_a_connected_peer_keeps_the_session_active() -> None:
    store = _store()
    created = _open(store, when=NOW - timedelta(hours=2))
    presence = PresenceBook()
    presence.attach(4, created.session.session_id)

    assert end_idle_sessions(store, presence, idle_seconds=900, now=NOW) == []

    presence.detach(4, created.session.session_id, NOW)
    assert (
        end_idle_sessions(store, presence, idle_seconds=900, now=NOW + timedelta(seconds=899)) == []
    )
    ended = end_idle_sessions(store, presence, idle_seconds=900, now=NOW + timedelta(seconds=900))
    assert ended == [created.session.session_id]


def test_one_remaining_peer_keeps_the_session_active() -> None:
    store = _store()
    created = _open(store, when=NOW - timedelta(hours=2))
    presence = PresenceBook()
    presence.attach(4, created.session.session_id)
    presence.attach(4, created.session.session_id)
    presence.detach(4, created.session.session_id, NOW)

    assert end_idle_sessions(store, presence, idle_seconds=900, now=NOW + timedelta(hours=1)) == []


def test_detach_without_a_peer_does_not_extend_the_idle_clock() -> None:
    store = _store()
    created = _open(store, when=NOW - timedelta(hours=2))
    presence = PresenceBook()
    presence.detach(4, created.session.session_id, NOW)

    assert end_idle_sessions(store, presence, idle_seconds=900, now=NOW) == [
        created.session.session_id
    ]


def test_restart_grace_keeps_an_existing_session_for_one_idle_interval() -> None:
    store = _store()
    created = _open(store, when=NOW - timedelta(hours=2))
    presence = PresenceBook()

    assert (
        end_idle_sessions(
            store,
            presence,
            idle_seconds=900,
            now=NOW,
            grant_unseen=True,
            process_started_at=NOW,
        )
        == []
    )
    assert (
        end_idle_sessions(
            store,
            presence,
            idle_seconds=900,
            now=NOW + timedelta(seconds=899),
            process_started_at=NOW,
        )
        == []
    )
    assert end_idle_sessions(
        store,
        presence,
        idle_seconds=900,
        now=NOW + timedelta(seconds=900),
        process_started_at=NOW,
    ) == [created.session.session_id]


def test_a_session_opened_after_startup_idles_from_its_start() -> None:
    store = _store()
    opened_at = NOW + timedelta(seconds=5)
    created = _open(store, when=opened_at)
    presence = PresenceBook()

    assert (
        end_idle_sessions(
            store,
            presence,
            idle_seconds=900,
            now=opened_at + timedelta(seconds=899),
            grant_unseen=True,
            process_started_at=NOW,
        )
        == []
    )
    assert end_idle_sessions(
        store,
        presence,
        idle_seconds=900,
        now=opened_at + timedelta(seconds=900),
        grant_unseen=True,
        process_started_at=NOW,
    ) == [created.session.session_id]
