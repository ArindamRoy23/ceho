"""Session lifecycle and final-transcript persistence boundary.

Control requests create, join, and end a tenant-scoped session. A connection
is not a user and not a speaker, so those rows stay untouched. Transcript
commits will share this module when that transaction is implemented.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ceho import models
from ceho.hot.credentials import issue_signaling_token

EndReason = Literal["explicit", "idle"]


class ControlError(Exception):
    """A control request that must not change the session."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SessionView:
    """Durable session fields the control API returns and idles on."""

    tenant_id: int
    session_id: int
    channel_tenant_id: int
    channel_id: int
    status: str
    started_at: datetime
    ended_at: datetime | None
    description: str | None


@dataclass(frozen=True)
class IssuedSession:
    """A session and the signaling credential minted for its connection."""

    session: SessionView
    signaling_token: str
    expires_at: datetime


class ControlStore(Protocol):
    """Persistence boundary for session control. Tests supply a fake."""

    def tenant_exists(self, tenant_id: int) -> bool:
        """Return whether the tenant row exists."""

    def find_channel(self, channel_id: int) -> tuple[int, int] | None:
        """Return channel tenant and channel id, or none when the row is missing."""

    def add_session(
        self,
        *,
        tenant_id: int,
        channel_tenant_id: int,
        channel_id: int,
        description: str | None,
        started_at: datetime,
    ) -> SessionView:
        """Stage an active session and return it with its new id."""

    def add_outbox(
        self,
        *,
        tenant_id: int,
        session_id: int,
        event_type: str,
        sequence: int,
        payload: dict[str, object],
    ) -> None:
        """Stage one pending outbox row for the current transaction."""

    def get_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Return the session in this tenant, or none."""

    def lock_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Lock the session row in this tenant and return it, or none."""

    def mark_ended(self, tenant_id: int, session_id: int, ended_at: datetime) -> None:
        """Stage status ended and ended_at on the locked session."""

    def next_outbox_sequence(self, tenant_id: int, session_id: int) -> int:
        """Return the next per-session outbox sequence."""

    def commit(self) -> None:
        """Commit the staged session change and outbox row together."""

    def rollback(self) -> None:
        """Drop the staged session change and outbox row."""

    def list_active(self) -> list[SessionView]:
        """Return active sessions across tenants."""


class PresenceBook:
    """In-process peer counts. Join is not a connected peer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peers: dict[tuple[int, int], int] = {}
        self._quiet_since: dict[tuple[int, int], datetime] = {}

    def attach(self, tenant_id: int, session_id: int) -> None:
        """Record one connected peer for a session."""
        key = (tenant_id, session_id)
        with self._lock:
            self._peers[key] = self._peers.get(key, 0) + 1
            self._quiet_since.pop(key, None)

    def detach(self, tenant_id: int, session_id: int, now: datetime) -> None:
        """Record one peer leaving. The idle clock starts when the last peer leaves."""
        key = (tenant_id, session_id)
        with self._lock:
            count = self._peers.get(key, 0)
            if count > 1:
                self._peers[key] = count - 1
                return
            self._peers.pop(key, None)
            if count == 1:
                self._quiet_since[key] = now

    def peer_count(self, tenant_id: int, session_id: int) -> int:
        """Return how many peers are connected for a session."""
        with self._lock:
            return self._peers.get((tenant_id, session_id), 0)

    def ready_to_end(
        self,
        tenant_id: int,
        session_id: int,
        started_at: datetime,
        now: datetime,
        idle_seconds: int,
        *,
        grant_unseen: bool,
        process_started_at: datetime | None,
    ) -> bool:
        """Return whether an active session with no peers has been idle long enough.

        The first sweep after startup grants one idle interval to sessions that
        already existed, and does not end them on that pass.
        """
        key = (tenant_id, session_id)
        with self._lock:
            if self._peers.get(key, 0) > 0:
                return False
            quiet = self._quiet_since.get(key)
            unseen_from_before_start = process_started_at is None or started_at < process_started_at
            if quiet is None and grant_unseen and unseen_from_before_start:
                self._quiet_since[key] = now
                return False
            since = quiet if quiet is not None else started_at
            return now - since >= timedelta(seconds=idle_seconds)


def create_session(
    store: ControlStore,
    *,
    tenant_id: int,
    channel_id: int,
    description: str | None,
    now: datetime,
    secret: str,
    ttl: timedelta,
) -> IssuedSession:
    """Insert an active session and its created event, then mint a signaling token."""
    _require_tenant(store, tenant_id)
    found = store.find_channel(channel_id)
    if found is None:
        raise ControlError("channel does not exist", status_code=404)
    channel_tenant_id, found_channel_id = found
    if channel_tenant_id != tenant_id and channel_tenant_id != models.GENERIC_TENANT_ID:
        raise ControlError("channel is not available to this tenant", status_code=403)
    try:
        session = store.add_session(
            tenant_id=tenant_id,
            channel_tenant_id=channel_tenant_id,
            channel_id=found_channel_id,
            description=_description(description),
            started_at=now,
        )
        store.add_outbox(
            tenant_id=tenant_id,
            session_id=session.session_id,
            event_type="session.created",
            sequence=0,
            payload={
                "tenant_id": tenant_id,
                "session_id": session.session_id,
                "channel_tenant_id": channel_tenant_id,
                "channel_id": found_channel_id,
                "started_at": now.isoformat(),
            },
        )
        store.commit()
    except Exception:
        store.rollback()
        raise
    token, expires_at = issue_signaling_token(
        secret,
        tenant_id=tenant_id,
        session_id=session.session_id,
        channel_tenant_id=channel_tenant_id,
        channel_id=found_channel_id,
        now=now,
        ttl=ttl,
    )
    return IssuedSession(session=session, signaling_token=token, expires_at=expires_at)


def join_session(
    store: ControlStore,
    *,
    tenant_id: int,
    session_id: int,
    now: datetime,
    secret: str,
    ttl: timedelta,
) -> IssuedSession:
    """Mint a new signaling token for an active session in this tenant."""
    _require_tenant(store, tenant_id)
    session = store.get_session(tenant_id, session_id)
    if session is None:
        raise ControlError("session does not exist", status_code=404)
    if session.status != "active":
        raise ControlError("session has ended", status_code=409)
    token, expires_at = issue_signaling_token(
        secret,
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        channel_tenant_id=session.channel_tenant_id,
        channel_id=session.channel_id,
        now=now,
        ttl=ttl,
    )
    return IssuedSession(session=session, signaling_token=token, expires_at=expires_at)


def end_session(
    store: ControlStore,
    *,
    tenant_id: int,
    session_id: int,
    now: datetime,
    reason: EndReason,
) -> SessionView:
    """End an active session and write one session.ended event."""
    _require_tenant(store, tenant_id)
    try:
        session = store.lock_session(tenant_id, session_id)
        if session is None:
            raise ControlError("session does not exist", status_code=404)
        if session.status != "active":
            raise ControlError("session is already ended", status_code=409)
        store.mark_ended(tenant_id, session_id, now)
        store.add_outbox(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="session.ended",
            sequence=store.next_outbox_sequence(tenant_id, session_id),
            payload={
                "tenant_id": tenant_id,
                "session_id": session_id,
                "channel_tenant_id": session.channel_tenant_id,
                "channel_id": session.channel_id,
                "ended_at": now.isoformat(),
                "reason": reason,
            },
        )
        store.commit()
    except Exception:
        store.rollback()
        raise
    return SessionView(
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        channel_tenant_id=session.channel_tenant_id,
        channel_id=session.channel_id,
        status="ended",
        started_at=session.started_at,
        ended_at=now,
        description=session.description,
    )


def end_idle_sessions(
    store: ControlStore,
    presence: PresenceBook,
    *,
    idle_seconds: int,
    now: datetime,
    grant_unseen: bool = False,
    process_started_at: datetime | None = None,
) -> list[int]:
    """End active sessions that have had no peer for the idle interval."""
    ended: list[int] = []
    for session in store.list_active():
        if not presence.ready_to_end(
            session.tenant_id,
            session.session_id,
            session.started_at,
            now,
            idle_seconds,
            grant_unseen=grant_unseen,
            process_started_at=process_started_at,
        ):
            continue
        try:
            end_session(
                store,
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                now=now,
                reason="idle",
            )
        except ControlError:
            continue
        ended.append(session.session_id)
    return ended


def _require_tenant(store: ControlStore, tenant_id: int) -> None:
    if not store.tenant_exists(tenant_id):
        raise ControlError("tenant does not exist", status_code=401)


def _description(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _session_view(row: models.Session) -> SessionView:
    return SessionView(
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        channel_tenant_id=row.channel_tenant_id,
        channel_id=row.channel_id,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
        description=row.session_description,
    )


class SqlControlStore:
    """SQLAlchemy persistence for session control."""

    def __init__(self, database: DbSession) -> None:
        self._db = database
        self._locked: models.Session | None = None

    def tenant_exists(self, tenant_id: int) -> bool:
        """Return whether the tenant row exists."""
        return self._db.get(models.Tenant, tenant_id) is not None

    def find_channel(self, channel_id: int) -> tuple[int, int] | None:
        """Return channel tenant and channel id, or none when the row is missing."""
        row = self._db.get(models.Channel, channel_id)
        if row is None:
            return None
        return (row.tenant_id, row.channel_id)

    def add_session(
        self,
        *,
        tenant_id: int,
        channel_tenant_id: int,
        channel_id: int,
        description: str | None,
        started_at: datetime,
    ) -> SessionView:
        """Stage an active session and return it with its new id."""
        row = models.Session(
            tenant_id=tenant_id,
            channel_tenant_id=channel_tenant_id,
            channel_id=channel_id,
            session_description=description,
            status="active",
            started_at=started_at,
        )
        self._db.add(row)
        self._db.flush()
        return _session_view(row)

    def add_outbox(
        self,
        *,
        tenant_id: int,
        session_id: int,
        event_type: str,
        sequence: int,
        payload: dict[str, object],
    ) -> None:
        """Stage one pending outbox row for the current transaction."""
        self._db.add(
            models.OutboxEvent(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=event_type,
                schema_version=1,
                sequence=sequence,
                payload=payload,
                publication_status="pending",
                attempt_count=0,
            )
        )

    def get_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Return the session in this tenant, or none."""
        row = self._db.scalar(
            select(models.Session).where(
                models.Session.tenant_id == tenant_id,
                models.Session.session_id == session_id,
            )
        )
        if row is None:
            return None
        return _session_view(row)

    def lock_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Lock the session row in this tenant and return it, or none."""
        row = self._db.scalar(
            select(models.Session)
            .where(
                models.Session.tenant_id == tenant_id,
                models.Session.session_id == session_id,
            )
            .with_for_update()
        )
        self._locked = row
        if row is None:
            return None
        return _session_view(row)

    def mark_ended(self, tenant_id: int, session_id: int, ended_at: datetime) -> None:
        """Stage status ended and ended_at on the locked session."""
        row = self._locked
        if row is None or row.tenant_id != tenant_id or row.session_id != session_id:
            raise RuntimeError("session is not locked")
        row.status = "ended"
        row.ended_at = ended_at

    def next_outbox_sequence(self, tenant_id: int, session_id: int) -> int:
        """Return the next per-session outbox sequence."""
        current = self._db.scalar(
            select(func.max(models.OutboxEvent.sequence)).where(
                models.OutboxEvent.tenant_id == tenant_id,
                models.OutboxEvent.session_id == session_id,
            )
        )
        if current is None:
            return 0
        return int(current) + 1

    def commit(self) -> None:
        """Commit the staged session change and outbox row together."""
        self._db.commit()
        self._locked = None

    def rollback(self) -> None:
        """Drop the staged session change and outbox row."""
        self._db.rollback()
        self._locked = None

    def list_active(self) -> list[SessionView]:
        """Return active sessions across tenants."""
        rows = self._db.scalars(
            select(models.Session).where(models.Session.status == "active")
        ).all()
        return [_session_view(row) for row in rows]
