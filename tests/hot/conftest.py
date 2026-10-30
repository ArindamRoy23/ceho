"""Fakes for hot-server control tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ceho.hot.config import HotServerSettings
from ceho.hot.session import SessionView


@dataclass(frozen=True)
class StoredOutbox:
    """One outbox row recorded by the in-memory control store."""

    tenant_id: int
    session_id: int
    event_type: str
    sequence: int
    payload: dict[str, object]
    publication_status: str
    schema_version: int


class MemoryControlStore:
    """In-memory control store. Staged rows appear only after commit."""

    def __init__(self) -> None:
        self.tenants: set[int] = set()
        self.channels: dict[int, tuple[int, int]] = {}
        self.sessions: dict[tuple[int, int], SessionView] = {}
        self.outbox: list[StoredOutbox] = []
        self.speakers: list[int] = []
        self.fail_commit = False
        self.fail_on_outbox = False
        self._next_id = 1
        self._pending_session: SessionView | None = None
        self._pending_outbox: list[StoredOutbox] = []
        self._pending_end: tuple[int, int, datetime] | None = None

    def allow(self, tenant_id: int, channel_id: int, channel_tenant_id: int | None = None) -> None:
        """Record a tenant and a channel that tenant may use."""
        self.tenants.add(tenant_id)
        owner = tenant_id if channel_tenant_id is None else channel_tenant_id
        self.channels[channel_id] = (owner, channel_id)

    def tenant_exists(self, tenant_id: int) -> bool:
        """Return whether the tenant row exists."""
        return tenant_id in self.tenants

    def find_channel(self, channel_id: int) -> tuple[int, int] | None:
        """Return channel tenant and channel id, or none when the row is missing."""
        return self.channels.get(channel_id)

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
        session = SessionView(
            tenant_id=tenant_id,
            session_id=self._next_id,
            channel_tenant_id=channel_tenant_id,
            channel_id=channel_id,
            status="active",
            started_at=started_at,
            ended_at=None,
            description=description,
        )
        self._next_id += 1
        self._pending_session = session
        return session

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
        if self.fail_on_outbox:
            raise RuntimeError("outbox failed")
        self._pending_outbox.append(
            StoredOutbox(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=event_type,
                sequence=sequence,
                payload=payload,
                publication_status="pending",
                schema_version=1,
            )
        )

    def get_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Return the session in this tenant, or none."""
        return self.sessions.get((tenant_id, session_id))

    def lock_session(self, tenant_id: int, session_id: int) -> SessionView | None:
        """Lock the session row in this tenant and return it, or none."""
        return self.sessions.get((tenant_id, session_id))

    def mark_ended(self, tenant_id: int, session_id: int, ended_at: datetime) -> None:
        """Stage status ended and ended_at on the locked session."""
        self._pending_end = (tenant_id, session_id, ended_at)

    def next_outbox_sequence(self, tenant_id: int, session_id: int) -> int:
        """Return the next per-session outbox sequence."""
        sequences = [
            row.sequence
            for row in (*self.outbox, *self._pending_outbox)
            if row.tenant_id == tenant_id and row.session_id == session_id
        ]
        if not sequences:
            return 0
        return max(sequences) + 1

    def commit(self) -> None:
        """Commit the staged session change and outbox row together."""
        if self.fail_commit:
            raise RuntimeError("commit failed")
        if self._pending_session is not None:
            row = self._pending_session
            self.sessions[(row.tenant_id, row.session_id)] = row
        if self._pending_end is not None:
            tenant_id, session_id, ended_at = self._pending_end
            current = self.sessions[(tenant_id, session_id)]
            self.sessions[(tenant_id, session_id)] = SessionView(
                tenant_id=current.tenant_id,
                session_id=current.session_id,
                channel_tenant_id=current.channel_tenant_id,
                channel_id=current.channel_id,
                status="ended",
                started_at=current.started_at,
                ended_at=ended_at,
                description=current.description,
            )
        self.outbox.extend(self._pending_outbox)
        self._clear_pending()

    def rollback(self) -> None:
        """Drop the staged session change and outbox row."""
        self._clear_pending()

    def list_active(self) -> list[SessionView]:
        """Return active sessions across tenants."""
        return [row for row in self.sessions.values() if row.status == "active"]

    def _clear_pending(self) -> None:
        self._pending_session = None
        self._pending_outbox = []
        self._pending_end = None


def hot_settings() -> HotServerSettings:
    """Return settings that never leave the test process."""
    return HotServerSettings(
        database_url="postgresql://ceho:ceho@localhost/ceho",
        media_storage_uri="s3://ceho-media",
        s3_endpoint_url=None,
        s3_access_key_id="ceho",
        s3_secret_access_key="ceho-dev-only",
        s3_region="us-east-1",
        signaling_secret="test-secret",
        event_publisher_url="memory://ceho",
        host="127.0.0.1",
        port=8000,
    )
