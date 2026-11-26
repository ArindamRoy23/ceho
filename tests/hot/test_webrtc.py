"""Direct-source WebRTC adapter: one microphone, ordered frames, bounded queue."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest

from ceho.hot.config import TransportQueueConfig
from ceho.hot.transport import (
    AuthorizedSessionBinding,
    CloseReason,
    InboundAudio,
    OrderedFrame,
    PeerClosed,
    PeerState,
    QueuePressure,
    SignalingError,
    SourceAdded,
    SourceRemoved,
    TransportError,
)
from ceho.hot.webrtc import DirectSourceWebRTC, SmallWebRTCPeer, WebRTCPeer, ice_servers_for

OFFER = "\r\n".join(
    [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=mid:0",
        "a=sendonly",
        "a=rtpmap:111 opus/48000/2",
        "",
    ]
)


def _binding(*, expires_at: datetime | None = None) -> AuthorizedSessionBinding:
    return AuthorizedSessionBinding(
        tenant_id=1,
        session_id=10,
        peer_id="peer-1",
        expires_at=expires_at or datetime(2026, 11, 26, tzinfo=timezone.utc),
    )


def _audio(
    timestamp_ms: int,
    *,
    duration_ms: int = 20,
    payload: bytes = b"\x00\x01",
    codec: str = "pcm_s16le",
    sample_rate: int = 16_000,
    channel_count: int = 1,
) -> InboundAudio:
    return InboundAudio(
        timestamp_ms=timestamp_ms,
        duration_ms=duration_ms,
        payload=payload,
        codec=codec,
        sample_rate=sample_rate,
        channel_count=channel_count,
    )


class _Clock:
    def __init__(self) -> None:
        self.now_ms = 0

    def __call__(self) -> int:
        return self.now_ms


class _Peer:
    """In-process stand-in for the Pipecat Small WebRTC peer."""

    def __init__(
        self,
        frames: list[InboundAudio] | None = None,
        *,
        answer: str = "v=0\r\n",
        negotiate_error: BaseException | None = None,
        frames_error: BaseException | None = None,
        pause_after: int | None = None,
        hold: bool = True,
    ) -> None:
        self._frames = list(frames or [])
        self._answer = answer
        self._negotiate_error = negotiate_error
        self._frames_error = frames_error
        self._pause_after = pause_after
        self._hold = hold
        self.offer: str | None = None
        self.closed = False
        self.opened = asyncio.Event()
        self.release = asyncio.Event()
        self.done = asyncio.Event()

    async def negotiate(self, offer_sdp: str) -> str:
        self.offer = offer_sdp
        if self._negotiate_error is not None:
            raise self._negotiate_error
        return self._answer

    async def frames(self) -> AsyncIterator[InboundAudio]:
        for index, frame in enumerate(self._frames):
            yield frame
            if self._pause_after is not None and index + 1 == self._pause_after:
                self.opened.set()
                await self.release.wait()
        if self._frames_error is not None:
            self.done.set()
            raise self._frames_error
        self.done.set()
        if self._hold:
            await self.release.wait()

    async def close(self) -> None:
        self.closed = True
        self.release.set()


def _adapter(
    peer: WebRTCPeer,
    *,
    binding: AuthorizedSessionBinding | None = None,
    config: TransportQueueConfig | None = None,
    now: datetime | None = None,
    clock: _Clock | None = None,
) -> DirectSourceWebRTC:
    current = now or datetime(2026, 11, 25, tzinfo=timezone.utc)
    return DirectSourceWebRTC(
        binding or _binding(),
        peer,
        config=config or TransportQueueConfig(),
        now=lambda: current,
        clock=clock or _Clock(),
    )


async def _finish(adapter: DirectSourceWebRTC, peer: _Peer) -> list[object]:
    await peer.done.wait()
    await adapter.close(CloseReason.CLIENT_DISCONNECT)
    return [event async for event in adapter.events()]


def test_offer_format_is_the_rtpmap_codec() -> None:
    from ceho.hot.webrtc import direct_microphone_format

    audio = direct_microphone_format(OFFER)

    assert audio.codec == "opus"
    assert audio.sample_rate == 48_000
    assert audio.channel_count == 2


def test_rtpmap_without_a_channel_count_is_mono() -> None:
    from ceho.hot.webrtc import direct_microphone_format

    sdp = "\r\n".join(
        [
            "v=0",
            "m=audio 9 RTP/AVP 0",
            "a=sendrecv",
            "a=rtpmap:0 PCMU/8000",
            "",
        ]
    )

    audio = direct_microphone_format(sdp)

    assert audio.codec == "pcmu"
    assert audio.sample_rate == 8_000
    assert audio.channel_count == 1


@pytest.mark.parametrize(
    "sdp",
    [
        "",
        "   ",
        "v=0\r\n",
        "\r\n".join(["v=0", "m=audio 9 UDP/TLS/RTP/SAVPF 111", "a=sendonly", ""]),
        "\r\n".join(
            [
                "v=0",
                "m=audio 9 UDP/TLS/RTP/SAVPF 111",
                "a=rtpmap:111 opus/48000/2",
                "m=audio 9 UDP/TLS/RTP/SAVPF 111",
                "a=rtpmap:111 opus/48000/2",
                "",
            ]
        ),
        "\r\n".join(
            [
                "v=0",
                "m=audio 9 UDP/TLS/RTP/SAVPF 111",
                "a=rtpmap:111 opus/48000/2",
                "m=video 9 UDP/TLS/RTP/SAVPF 96",
                "a=rtpmap:96 VP8/90000",
                "",
            ]
        ),
        "\r\n".join(
            [
                "v=0",
                "m=video 9 UDP/TLS/RTP/SAVPF 96",
                "a=rtpmap:96 VP8/90000",
                "",
            ]
        ),
        "\r\n".join(
            [
                "v=0",
                "m=audio 0 UDP/TLS/RTP/SAVPF 111",
                "a=sendonly",
                "a=rtpmap:111 opus/48000/2",
                "",
            ]
        ),
        "\r\n".join(
            [
                "v=0",
                "m=audio 9 UDP/TLS/RTP/SAVPF 111",
                "a=recvonly",
                "a=rtpmap:111 opus/48000/2",
                "",
            ]
        ),
        "\r\n".join(
            [
                "v=0",
                "m=audio 9 UDP/TLS/RTP/SAVPF 111",
                "a=inactive",
                "a=rtpmap:111 opus/48000/2",
                "",
            ]
        ),
        "\r\n".join(
            [
                "v=0",
                "a=sendrecv",
                "m=audio 9 UDP/TLS/RTP/SAVPF 111",
                "a=recvonly",
                "a=rtpmap:111 opus/48000/2",
                "",
            ]
        ),
    ],
)
async def test_offer_must_describe_one_sending_microphone(sdp: str) -> None:
    peer = _Peer()
    adapter = _adapter(peer)

    with pytest.raises(SignalingError, match="offer must describe one sending audio source"):
        await adapter.accept_offer(sdp)

    assert peer.offer is None
    assert peer.closed is True
    events = [event async for event in adapter.events()]
    assert events == [
        PeerClosed(
            tenant_id=1,
            session_id=10,
            peer_id="peer-1",
            reason=CloseReason.SIGNALING_REJECTED,
            state=PeerState.CLOSED,
        )
    ]


async def test_session_level_sendonly_is_a_microphone() -> None:
    sdp = "\r\n".join(
        [
            "v=0",
            "a=sendonly",
            "m=audio 9 UDP/TLS/RTP/SAVPF 111",
            "a=rtpmap:111 opus/48000/2",
            "",
        ]
    )
    peer = _Peer([_audio(0)])
    adapter = _adapter(peer)

    answer = await adapter.accept_offer(sdp)
    events = await _finish(adapter, peer)

    assert answer == "v=0\r\n"
    assert peer.offer == sdp
    assert isinstance(events[0], SourceAdded)


async def test_authorized_offer_establishes_one_direct_source() -> None:
    peer = _Peer([_audio(0), _audio(20)])
    adapter = _adapter(peer)

    answer = await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    assert answer == "v=0\r\n"
    assert adapter.state is PeerState.CLOSED
    added = events[0]
    assert isinstance(added, SourceAdded)
    assert added.tenant_id == 1
    assert added.session_id == 10
    assert added.peer_id == "peer-1"
    assert added.source_id == "peer-1/microphone"
    assert added.codec == "opus"
    assert added.sample_rate == 48_000
    assert added.channel_count == 2
    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert [frame.sequence for frame in frames] == [1, 2]
    assert [frame.timestamp_ms for frame in frames] == [0, 20]
    assert [frame.codec for frame in frames] == ["pcm_s16le", "pcm_s16le"]
    assert [frame.sample_rate for frame in frames] == [16_000, 16_000]
    assert [frame.channel_count for frame in frames] == [1, 1]
    assert [frame.payload for frame in frames] == [b"\x00\x01", b"\x00\x01"]
    assert [frame.discontinuity for frame in frames] == [False, False]
    assert [frame.gap_ms for frame in frames] == [0, 0]
    assert [frame.source_id for frame in frames] == [
        "peer-1/microphone",
        "peer-1/microphone",
    ]
    removed = next(event for event in events if isinstance(event, SourceRemoved))
    closed = events[-1]
    assert isinstance(closed, PeerClosed)
    assert removed.reason is CloseReason.CLIENT_DISCONNECT
    assert removed.source_id == "peer-1/microphone"
    assert closed.reason is CloseReason.CLIENT_DISCONNECT
    assert closed.state is PeerState.CLOSED
    assert events.index(removed) < events.index(closed)
    assert all(
        events.index(event) < events.index(removed)
        for event in events
        if isinstance(event, OrderedFrame)
    )


async def test_a_timestamp_gap_is_preserved_and_marked() -> None:
    peer = _Peer([_audio(0), _audio(20), _audio(80)])
    adapter = _adapter(peer)

    await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert [(frame.timestamp_ms, frame.discontinuity, frame.gap_ms) for frame in frames] == [
        (0, False, 0),
        (20, False, 0),
        (80, True, 40),
    ]


async def test_a_backwards_timestamp_is_a_discontinuity() -> None:
    peer = _Peer([_audio(100), _audio(10)])
    adapter = _adapter(peer)

    await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert frames[1].timestamp_ms == 10
    assert frames[1].discontinuity is True
    assert frames[1].gap_ms == 0


async def test_an_overlapping_timestamp_is_a_discontinuity() -> None:
    peer = _Peer([_audio(0, duration_ms=20), _audio(10, duration_ms=20)])
    adapter = _adapter(peer)

    await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert frames[1].timestamp_ms == 10
    assert frames[1].discontinuity is True
    assert frames[1].gap_ms == 0


async def test_queue_overflow_drops_the_oldest_frame() -> None:
    peer = _Peer([_audio(0), _audio(20), _audio(40)])
    adapter = _adapter(peer, config=TransportQueueConfig(max_queued_frames=1))

    await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    pressures = [event for event in events if isinstance(event, QueuePressure)]
    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert len(pressures) == 1
    assert pressures[0].reason == "queue_full"
    assert pressures[0].dropped_frames == 2
    assert pressures[0].dropped_sequence == 2
    assert pressures[0].queued_frames == 1
    assert pressures[0].max_queued_frames == 1
    assert pressures[0].source_id == "peer-1/microphone"
    assert [(frame.sequence, frame.timestamp_ms, frame.discontinuity) for frame in frames] == [
        (3, 40, True)
    ]
    assert events.index(pressures[0]) < events.index(
        next(event for event in events if isinstance(event, OrderedFrame))
    )


async def test_a_frame_older_than_the_age_limit_is_dropped() -> None:
    clock = _Clock()
    peer = _Peer([_audio(0), _audio(20)], pause_after=1)
    adapter = _adapter(
        peer,
        config=TransportQueueConfig(max_frame_age_ms=1_000),
        clock=clock,
    )

    await adapter.accept_offer(OFFER)
    await peer.opened.wait()
    clock.now_ms = 1_001
    peer.release.set()
    events = await _finish(adapter, peer)

    pressures = [event for event in events if isinstance(event, QueuePressure)]
    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert [pressure.reason for pressure in pressures] == ["frame_age"]
    assert pressures[0].dropped_sequence == 1
    assert [(frame.sequence, frame.discontinuity, frame.timestamp_ms) for frame in frames] == [
        (2, True, 20)
    ]


async def test_a_frame_at_the_age_limit_stays_queued() -> None:
    clock = _Clock()
    peer = _Peer([_audio(0), _audio(20)], pause_after=1)
    adapter = _adapter(
        peer,
        config=TransportQueueConfig(max_frame_age_ms=1_000),
        clock=clock,
    )

    await adapter.accept_offer(OFFER)
    await peer.opened.wait()
    clock.now_ms = 1_000
    peer.release.set()
    events = await _finish(adapter, peer)

    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert [event for event in events if isinstance(event, QueuePressure)] == []
    assert [frame.sequence for frame in frames] == [1, 2]
    assert frames[1].discontinuity is False


@pytest.mark.parametrize(
    "frame",
    [
        _audio(0, payload=b""),
        _audio(-1),
        _audio(0, duration_ms=0),
        _audio(0, codec=""),
        _audio(0, codec=" pcm "),
        _audio(0, sample_rate=0),
        _audio(0, channel_count=0),
    ],
)
async def test_an_invalid_frame_is_not_delivered(frame: InboundAudio) -> None:
    peer = _Peer([frame, _audio(20)])
    adapter = _adapter(peer)

    await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    pressures = [event for event in events if isinstance(event, QueuePressure)]
    assert [item.sequence for item in frames] == [1]
    assert frames[0].timestamp_ms == 20
    assert frames[0].discontinuity is True
    assert len(pressures) == 1
    assert pressures[0].reason == "invalid_frame"
    assert pressures[0].dropped_sequence is None


async def test_an_oversized_frame_is_not_queued() -> None:
    peer = _Peer([_audio(0, payload=b"\x00\x01"), _audio(20, payload=b"\x02\x03")])
    adapter = _adapter(peer, config=TransportQueueConfig(max_frame_bytes=1))

    await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    frames = [event for event in events if isinstance(event, OrderedFrame)]
    pressures = [event for event in events if isinstance(event, QueuePressure)]
    assert frames == []
    assert len(pressures) == 1
    assert pressures[0].reason == "frame_too_large"
    assert pressures[0].dropped_frames == 2
    assert pressures[0].dropped_sequence == 2
    assert pressures[0].queued_frames == 0


async def test_expired_binding_rejects_signaling() -> None:
    expires_at = datetime(2026, 11, 25, tzinfo=timezone.utc)
    peer = _Peer([_audio(0)])
    adapter = _adapter(
        peer,
        binding=_binding(expires_at=expires_at),
        now=expires_at,
    )

    with pytest.raises(SignalingError, match="join credential expired"):
        await adapter.accept_offer(OFFER)

    assert peer.offer is None
    events = [event async for event in adapter.events()]
    assert events == [
        PeerClosed(
            tenant_id=1,
            session_id=10,
            peer_id="peer-1",
            reason=CloseReason.SIGNALING_REJECTED,
            state=PeerState.CLOSED,
        )
    ]


async def test_a_binding_is_accepted_until_its_expiry() -> None:
    expires_at = datetime(2026, 11, 25, tzinfo=timezone.utc)
    peer = _Peer([_audio(0)])
    adapter = _adapter(
        peer,
        binding=_binding(expires_at=expires_at),
        now=expires_at - timedelta(milliseconds=1),
    )

    await adapter.accept_offer(OFFER)
    events = await _finish(adapter, peer)

    assert isinstance(events[0], SourceAdded)


async def test_negotiate_failure_closes_the_peer() -> None:
    peer = _Peer(negotiate_error=RuntimeError("ice failed"))
    adapter = _adapter(peer)

    with pytest.raises(SignalingError, match="signaling failed"):
        await adapter.accept_offer(OFFER)

    assert peer.closed is True
    events = [event async for event in adapter.events()]
    assert [type(event) for event in events] == [PeerClosed]
    assert isinstance(events[0], PeerClosed)
    assert events[0].reason is CloseReason.SIGNALING_REJECTED


async def test_an_empty_answer_closes_the_peer() -> None:
    peer = _Peer(answer="  ")
    adapter = _adapter(peer)

    with pytest.raises(SignalingError, match="signaling produced no answer"):
        await adapter.accept_offer(OFFER)

    events = [event async for event in adapter.events()]
    assert isinstance(events[0], PeerClosed)
    assert events[0].reason is CloseReason.SIGNALING_REJECTED


async def test_a_second_offer_is_rejected() -> None:
    peer = _Peer([_audio(0)])
    adapter = _adapter(peer)
    await adapter.accept_offer(OFFER)

    with pytest.raises(TransportError, match="peer is not accepting signaling"):
        await adapter.accept_offer(OFFER)

    events = await _finish(adapter, peer)
    assert [event for event in events if isinstance(event, SourceAdded)]
    assert peer.closed is True


async def test_media_failure_stops_delivery() -> None:
    peer = _Peer([_audio(0), _audio(20)], frames_error=RuntimeError("ice broke"))
    adapter = _adapter(peer)

    await adapter.accept_offer(OFFER)
    await adapter.wait_closed()
    events = [event async for event in adapter.events()]

    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert [frame.sequence for frame in frames] == [1, 2]
    removed = next(event for event in events if isinstance(event, SourceRemoved))
    closed = events[-1]
    assert isinstance(closed, PeerClosed)
    assert removed.reason is CloseReason.MEDIA_FAILED
    assert closed.reason is CloseReason.MEDIA_FAILED
    assert events.index(removed) < events.index(closed)
    assert peer.closed is True


async def test_track_end_removes_the_source_and_leaves_the_peer_until_close() -> None:
    peer = _Peer([_audio(0)], hold=False)
    adapter = _adapter(peer)
    seen: list[object] = []

    async def consume() -> None:
        async for event in adapter.events():
            seen.append(event)

    consumer = asyncio.create_task(consume())
    await adapter.accept_offer(OFFER)
    await peer.done.wait()
    for _ in range(20):
        if any(isinstance(event, SourceRemoved) for event in seen):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("source was not removed")

    assert adapter.state is PeerState.CONNECTED
    await adapter.close(CloseReason.SERVER_CLOSE)
    await consumer

    removed = next(event for event in seen if isinstance(event, SourceRemoved))
    closed = seen[-1]
    assert isinstance(removed, SourceRemoved)
    assert removed.reason is CloseReason.SOURCE_ENDED
    assert isinstance(closed, PeerClosed)
    assert closed.reason is CloseReason.SERVER_CLOSE


async def test_close_before_signaling_emits_peer_closed_without_a_source() -> None:
    peer = _Peer([_audio(0)])
    adapter = _adapter(peer)

    await adapter.close(CloseReason.SERVER_CLOSE)
    events = [event async for event in adapter.events()]

    assert events == [
        PeerClosed(
            tenant_id=1,
            session_id=10,
            peer_id="peer-1",
            reason=CloseReason.SERVER_CLOSE,
            state=PeerState.CLOSED,
        )
    ]
    with pytest.raises(TransportError, match="peer is not accepting signaling"):
        await adapter.accept_offer(OFFER)
    assert peer.offer is None


async def test_close_is_idempotent() -> None:
    peer = _Peer([_audio(0)])
    adapter = _adapter(peer)
    await adapter.accept_offer(OFFER)
    await peer.done.wait()

    await adapter.close(CloseReason.SERVER_CLOSE)
    await adapter.close(CloseReason.CLIENT_DISCONNECT)
    events = [event async for event in adapter.events()]

    closed = [event for event in events if isinstance(event, PeerClosed)]
    assert len(closed) == 1
    assert closed[0].reason is CloseReason.SERVER_CLOSE


async def test_remote_disconnect_stops_delivery(caplog: pytest.LogCaptureFixture) -> None:
    from ceho.hot.transport import SourceDisconnected

    caplog.set_level("DEBUG", logger="ceho.hot.webrtc")
    peer = _Peer(
        [_audio(0), _audio(40)],
        frames_error=SourceDisconnected(CloseReason.CLIENT_DISCONNECT),
    )
    adapter = _adapter(peer)

    await adapter.accept_offer(OFFER)
    await adapter.wait_closed()
    events = [event async for event in adapter.events()]

    frames = [event.frame for event in events if isinstance(event, OrderedFrame)]
    assert [frame.timestamp_ms for frame in frames] == [0, 40]
    assert frames[1].discontinuity is True
    assert frames[1].gap_ms == 20
    assert isinstance(events[-2], SourceRemoved)
    assert isinstance(events[-1], PeerClosed)
    assert events[-1].reason is CloseReason.CLIENT_DISCONNECT
    messages = [record.msg for record in caplog.records if record.name == "ceho.hot.webrtc"]
    assert "peer connected" in messages
    assert "source added" in messages
    assert "audio frame accepted" in messages
    assert "source removed" in messages
    assert "peer closed" in messages


async def test_queue_pressure_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="ceho.hot.webrtc")
    peer = _Peer([_audio(0), _audio(20)])
    adapter = _adapter(peer, config=TransportQueueConfig(max_queued_frames=1))

    await adapter.accept_offer(OFFER)
    await _finish(adapter, peer)

    warnings = [
        record
        for record in caplog.records
        if record.name == "ceho.hot.webrtc" and record.msg == "transport queue pressure"
    ]
    assert warnings
    assert warnings[0].ceho_fields["reason"] == "queue_full"  # type: ignore[attr-defined]
    assert warnings[0].ceho_fields["source_id"] == "peer-1/microphone"  # type: ignore[attr-defined]


async def test_rejected_offer_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("ERROR", logger="ceho.hot.webrtc")
    adapter = _adapter(_Peer())

    with pytest.raises(SignalingError):
        await adapter.accept_offer("v=0\r\n")

    records = [record for record in caplog.records if record.msg == "signaling rejected"]
    assert records
    assert records[0].ceho_fields["error"] == "offer must describe one sending audio source"  # type: ignore[attr-defined]
    assert records[0].ceho_fields["peer_id"] == "peer-1"  # type: ignore[attr-defined]


async def test_media_failure_is_logged_with_the_exception(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("ERROR", logger="ceho.hot.webrtc")
    peer = _Peer([_audio(0)], frames_error=RuntimeError("ice broke"))
    adapter = _adapter(peer)

    await adapter.accept_offer(OFFER)
    await adapter.wait_closed()

    records = [record for record in caplog.records if record.msg == "peer media failed"]
    assert records
    assert records[0].exc_info is not None
    assert records[0].ceho_fields["peer_id"] == "peer-1"  # type: ignore[attr-defined]


async def test_small_webrtc_peer_gathers_with_stun_and_turn() -> None:
    from ceho.hot.turn import IceRelay

    relay = IceRelay(
        stun_url="stun:stun.cloudflare.com:3478",
        turn_url="turns:turn.cloudflare.com:443?transport=tcp",
        username="turn-user",
        password="turn-secret",
    )
    peer = SmallWebRTCPeer(ice_servers=ice_servers_for(relay))
    try:
        servers = peer.ice_servers
        assert servers[0].urls == relay.stun_url
        assert servers[1].urls == relay.turn_url
        assert servers[1].username == relay.username
        assert servers[1].credential == relay.password
    finally:
        await peer.close()


async def test_small_webrtc_loopback_delivers_pcm_from_one_microphone() -> None:
    from fractions import Fraction

    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamTrack
    from av import AudioFrame

    class _Tone(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._pts = 0

        async def recv(self) -> AudioFrame:
            frame = AudioFrame(format="s16", layout="mono", samples=160)
            for plane in frame.planes:
                plane.update(b"\x01\x00" * frame.samples)
            frame.pts = self._pts
            frame.sample_rate = 8000
            frame.time_base = Fraction(1, 8000)
            self._pts += frame.samples
            await asyncio.sleep(0)
            return frame

    offer = RTCPeerConnection()
    offer.addTrack(_Tone())
    await offer.setLocalDescription(await offer.createOffer())
    description = offer.localDescription
    assert description is not None
    adapter = _adapter(SmallWebRTCPeer())
    seen: list[object] = []
    got_frame = asyncio.Event()

    async def consume() -> None:
        async for event in adapter.events():
            seen.append(event)
            if isinstance(event, OrderedFrame):
                got_frame.set()

    consumer = asyncio.create_task(consume())
    try:
        answer = await adapter.accept_offer(description.sdp)
        await offer.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        await asyncio.wait_for(got_frame.wait(), 10)
    finally:
        await adapter.close()
        await offer.close()
        await consumer

    added = seen[0]
    assert isinstance(added, SourceAdded)
    assert added.source_id == "peer-1/microphone"
    assert added.codec
    frame = next(event.frame for event in seen if isinstance(event, OrderedFrame))
    assert frame.codec == "pcm_s16le"
    assert frame.payload
    assert frame.sample_rate > 0
    assert frame.channel_count >= 1
    assert frame.sequence >= 1
    assert isinstance(seen[-1], PeerClosed)
