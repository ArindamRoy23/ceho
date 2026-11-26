"""Transport port and normalized media contracts.

The hot server authorizes a session, then hands this port an already
authorized binding. The port does not look up tenants or decide who may
join. ``webrtc.py`` is the first adapter. It exposes one inbound direct
microphone and does not write transcripts, recordings, or outbox rows.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Protocol

from ceho.hot.config import TransportQueueConfig


class TransportError(RuntimeError):
    """Raised when a transport peer is used outside its contract."""


class SignalingError(TransportError):
    """Raised when signaling does not establish the one direct source."""


class PeerState(Enum):
    """Lifecycle of one transport peer after signaling."""

    CONNECTED = "connected"
    CLOSING = "closing"
    CLOSED = "closed"


class CloseReason(Enum):
    """Why a source or peer stopped delivering media."""

    CLIENT_DISCONNECT = "client_disconnect"
    SERVER_CLOSE = "server_close"
    SIGNALING_REJECTED = "signaling_rejected"
    MEDIA_FAILED = "media_failed"
    SOURCE_ENDED = "source_ended"


class SourceDisconnected(Exception):
    """The remote peer stopped the media connection."""

    def __init__(self, reason: CloseReason, *, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail or reason.value
        super().__init__(self.detail)


@dataclass(frozen=True)
class AuthorizedSessionBinding:
    """Session membership the server has already authorized for signaling.

    ``expires_at`` is the join credential's expiry. The adapter refuses
    signaling at that instant. It does not inspect the credential itself.
    """

    tenant_id: int
    session_id: int
    peer_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.tenant_id < 1 or self.session_id < 1 or not _peer_id(self.peer_id):
            raise TransportError("binding must name a tenant, session, and peer")
        if self.expires_at.tzinfo is None:
            raise TransportError("binding expiry must be timezone-aware")


@dataclass(frozen=True)
class AudioFormat:
    """Codec declaration for one direct source."""

    codec: str
    sample_rate: int
    channel_count: int


@dataclass(frozen=True)
class InboundAudio:
    """One media chunk as the peer connection delivered it."""

    timestamp_ms: int
    duration_ms: int
    payload: bytes
    codec: str
    sample_rate: int
    channel_count: int


@dataclass(frozen=True)
class AudioFrame:
    """One ordered frame at the pipeline boundary.

    ``timestamp_ms`` is the channel media clock. A gap or a dropped frame sets
    ``discontinuity`` and leaves that clock unchanged. ``gap_ms`` is the
    forward hole on that clock since the previous accepted frame. A backwards
    or overlapping timestamp is a discontinuity with ``gap_ms`` of zero.
    """

    source_id: str
    sequence: int
    timestamp_ms: int
    duration_ms: int
    codec: str
    sample_rate: int
    channel_count: int
    payload: bytes
    discontinuity: bool
    gap_ms: int


@dataclass(frozen=True)
class SourceAdded:
    """The peer's one inbound microphone is ready."""

    tenant_id: int
    session_id: int
    peer_id: str
    source_id: str
    codec: str
    sample_rate: int
    channel_count: int


@dataclass(frozen=True)
class OrderedFrame:
    """The next frame for one source, in source sequence order."""

    tenant_id: int
    session_id: int
    peer_id: str
    frame: AudioFrame


@dataclass(frozen=True)
class SourceRemoved:
    """The microphone stopped. The session row is unchanged."""

    tenant_id: int
    session_id: int
    peer_id: str
    source_id: str
    reason: CloseReason


@dataclass(frozen=True)
class PeerClosed:
    """The peer will emit no further events."""

    tenant_id: int
    session_id: int
    peer_id: str
    reason: CloseReason
    state: PeerState


@dataclass(frozen=True)
class QueuePressure:
    """Frames were dropped instead of growing the inbound queue."""

    tenant_id: int
    session_id: int
    peer_id: str
    source_id: str
    dropped_sequence: int | None
    dropped_frames: int
    queued_frames: int
    max_queued_frames: int
    reason: str


TransportEvent = SourceAdded | OrderedFrame | SourceRemoved | PeerClosed | QueuePressure


class DirectSourceTransport(Protocol):
    """What ``pipeline.py`` consumes. Signaling stays on the adapter."""

    def events(self) -> AsyncIterator[TransportEvent]:
        """Yield source, frame, pressure, and close events until the peer ends."""

    async def close(self, reason: CloseReason = CloseReason.SERVER_CLOSE) -> None:
        """Stop delivery. Already-queued frames are still delivered first."""


def direct_source_id(peer_id: str) -> str:
    """Return the one microphone id for a peer."""
    return f"{peer_id}/microphone"


@dataclass(frozen=True)
class Drop:
    """One frame that was not left in the queue."""

    reason: str
    sequence: int | None
    queued_frames: int


@dataclass(frozen=True)
class PushResult:
    """The frame that stayed queued, and every drop this push caused.

    ``queued_frames`` is the depth after the push, not the depth mid-drop.
    """

    queued: AudioFrame | None
    drops: tuple[Drop, ...]
    queued_frames: int


class _Pressure:
    event: QueuePressure | None = None


@dataclass
class _Queued:
    event: OrderedFrame
    enqueued_at_ms: int


class InboundQueue:
    """Bounded ordered frames for one direct source.

    Overflow and age drops remove the oldest queued frame. The next frame
    that is still delivered is marked as a discontinuity. The channel
    timestamp is not rewritten. A frame that does not fit ``max_frame_bytes``
    is not stored.
    """

    def __init__(
        self,
        config: TransportQueueConfig,
        clock: Callable[[], int],
        *,
        tenant_id: int,
        session_id: int,
        peer_id: str,
        source_id: str,
    ) -> None:
        self._config = config
        self._clock = clock
        self._tenant_id = tenant_id
        self._session_id = session_id
        self._peer_id = peer_id
        self._source_id = source_id
        self._items: deque[SourceAdded | _Queued | _Pressure | SourceRemoved | PeerClosed] = deque()
        self._pressure = _Pressure()
        self._pressure_queued = False
        self._sequence = 1
        self._last: tuple[int, int] | None = None
        self._missed = False
        self._open = True
        self.sealed = False

    def add_source(self, event: SourceAdded) -> None:
        self._items.append(event)

    def push(self, audio: InboundAudio) -> PushResult | None:
        """Queue one chunk. ``None`` means the source is already closed."""
        if not self._open:
            return None
        drops = self._expire()
        if _invalid(audio):
            self._missed = True
            drops.append(Drop("invalid_frame", None, self._queued_frames()))
        elif len(audio.payload) > self._config.max_frame_bytes:
            sequence = self._sequence
            self._sequence += 1
            self._missed = True
            drops.append(Drop("frame_too_large", sequence, self._queued_frames()))
        else:
            while self._queued_frames() >= self._config.max_queued_frames:
                drops.append(self._drop_oldest("queue_full"))
            frame = self._make(audio)
            self._items.append(_Queued(self._ordered(frame), self._clock()))
            self._remember(drops)
            return PushResult(frame, tuple(drops), self._queued_frames())
        self._remember(drops)
        return PushResult(None, tuple(drops), self._queued_frames())

    def pop(self) -> tuple[TransportEvent | None, tuple[Drop, ...]]:
        """Return the next event and any frames that aged out while waiting."""
        drops = self._expire()
        self._remember(drops)
        if not self._items:
            return None, tuple(drops)
        item = self._items.popleft()
        if isinstance(item, _Pressure):
            self._pressure_queued = False
            event = item.event
            item.event = None
            return event, tuple(drops)
        if isinstance(item, _Queued):
            return item.event, tuple(drops)
        return item, tuple(drops)

    def remove_source(self, event: SourceRemoved) -> None:
        """Stop accepting media. Queued frames stay in front of this event."""
        self._open = False
        self._items.append(event)

    def close_peer(self, removed: SourceRemoved | None, closed: PeerClosed) -> None:
        """Refuse further media and append the terminal events."""
        self._open = False
        if removed is not None:
            self._items.append(removed)
        self._items.append(closed)
        self.sealed = True

    def _make(self, audio: InboundAudio) -> AudioFrame:
        discontinuity, gap_ms = _timing(self._last, audio.timestamp_ms)
        if self._missed:
            discontinuity = True
            self._missed = False
        self._last = (audio.timestamp_ms, audio.duration_ms)
        sequence = self._sequence
        self._sequence += 1
        return AudioFrame(
            source_id=self._source_id,
            sequence=sequence,
            timestamp_ms=audio.timestamp_ms,
            duration_ms=audio.duration_ms,
            codec=audio.codec,
            sample_rate=audio.sample_rate,
            channel_count=audio.channel_count,
            payload=audio.payload,
            discontinuity=discontinuity,
            gap_ms=gap_ms,
        )

    def _ordered(self, frame: AudioFrame) -> OrderedFrame:
        return OrderedFrame(self._tenant_id, self._session_id, self._peer_id, frame)

    def _expire(self) -> list[Drop]:
        now_ms = self._clock()
        drops: list[Drop] = []
        while True:
            oldest = self._oldest()
            if oldest is None:
                return drops
            if now_ms - oldest.enqueued_at_ms <= self._config.max_frame_age_ms:
                return drops
            drops.append(self._drop_oldest("frame_age"))

    def _drop_oldest(self, reason: str) -> Drop:
        for index, item in enumerate(self._items):
            if isinstance(item, _Queued):
                del self._items[index]
                self._mark_gap(index)
                return Drop(reason, item.event.frame.sequence, self._queued_frames())
        raise TransportError("transport queue dropped a frame that was not queued")

    def _mark_gap(self, index: int) -> None:
        for cursor in range(index, len(self._items)):
            item = self._items[cursor]
            if isinstance(item, _Queued):
                frame = replace(item.event.frame, discontinuity=True)
                item.event = self._ordered(frame)
                return
        self._missed = True

    def _oldest(self) -> _Queued | None:
        for item in self._items:
            if isinstance(item, _Queued):
                return item
        return None

    def _queued_frames(self) -> int:
        return sum(isinstance(item, _Queued) for item in self._items)

    def _remember(self, drops: list[Drop]) -> None:
        if not drops:
            return
        previous = self._pressure.event.dropped_frames if self._pressure.event is not None else 0
        last = drops[-1]
        self._pressure.event = QueuePressure(
            tenant_id=self._tenant_id,
            session_id=self._session_id,
            peer_id=self._peer_id,
            source_id=self._source_id,
            dropped_sequence=last.sequence,
            dropped_frames=previous + len(drops),
            queued_frames=self._queued_frames(),
            max_queued_frames=self._config.max_queued_frames,
            reason=last.reason,
        )
        if self._pressure_queued:
            return
        self._pressure_queued = True
        for index, item in enumerate(self._items):
            if isinstance(item, _Queued):
                self._items.insert(index, self._pressure)
                return
        for index, item in enumerate(self._items):
            if isinstance(item, (SourceRemoved, PeerClosed)):
                self._items.insert(index, self._pressure)
                return
        self._items.append(self._pressure)


def _peer_id(value: str) -> bool:
    return bool(value) and value == value.strip() and "/" not in value and "\\" not in value


def _invalid(audio: InboundAudio) -> bool:
    if audio.timestamp_ms < 0 or audio.duration_ms < 1 or not audio.payload:
        return True
    if not audio.codec or audio.codec != audio.codec.strip():
        return True
    return audio.sample_rate < 1 or audio.channel_count < 1


def _timing(previous: tuple[int, int] | None, timestamp_ms: int) -> tuple[bool, int]:
    if previous is None:
        return False, 0
    previous_timestamp, previous_duration = previous
    expected = previous_timestamp + previous_duration
    if timestamp_ms < expected:
        return True, 0
    gap_ms = timestamp_ms - expected
    if gap_ms > 0:
        return True, gap_ms
    return False, 0
