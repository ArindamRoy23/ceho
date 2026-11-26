"""WebRTC adapter for one authenticated direct microphone.

Signaling receives an authorized session binding from the server. The offer
must describe exactly one sending audio source. Pipecat's
``SmallWebRTCConnection`` owns the peer connection. Media is queued with
``TransportQueueConfig`` and delivered as ordered frames. This module does
not authorize tenants, run the pipeline, record audio, or send talkback.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any, NoReturn, Protocol

from aiortc.rtcconfiguration import RTCIceServer
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

from ceho.hot.config import TransportQueueConfig
from ceho.hot.transport import (
    AudioFormat,
    AuthorizedSessionBinding,
    CloseReason,
    Drop,
    InboundAudio,
    InboundQueue,
    OrderedFrame,
    PeerClosed,
    PeerState,
    SignalingError,
    SourceAdded,
    SourceDisconnected,
    SourceRemoved,
    TransportError,
    TransportEvent,
    direct_source_id,
)
from ceho.hot.turn import IceRelay
from ceho.logging import get_logger

logger = get_logger(__name__)

_OFFER_REJECTION = "offer must describe one sending audio source"
_DIRECTIONS = frozenset({"sendonly", "sendrecv", "recvonly", "inactive"})


class WebRTCPeer(Protocol):
    """One peer connection. Tests supply a stand-in; production uses Small WebRTC."""

    async def negotiate(self, offer_sdp: str) -> str:
        """Apply an offer and return the answer SDP."""

    def frames(self) -> AsyncIterator[InboundAudio]:
        """Yield inbound audio until the track ends."""

    async def close(self) -> None:
        """Release the peer connection."""


def direct_microphone_format(sdp: str) -> AudioFormat:
    """Return the codec declared by an offer with one sending audio source.

    Any other shape is rejected. The adapter does not invent a second source
    for an extra m-line, and it does not accept a microphone that is not sending.
    """
    lines = _sdp_lines(sdp)
    media_at = [index for index, line in enumerate(lines) if line.startswith("m=")]
    if len(media_at) != 1:
        raise SignalingError(_OFFER_REJECTION)
    start = media_at[0]
    parts = lines[start].split()
    if len(parts) < 4 or parts[0].lower() != "m=audio" or parts[1] == "0":
        raise SignalingError(_OFFER_REJECTION)
    direction = _direction(lines[start + 1 :]) or _direction(lines[:start]) or "sendrecv"
    if direction in {"recvonly", "inactive"}:
        raise SignalingError(_OFFER_REJECTION)
    declared = _rtpmap(lines[start + 1 :], parts[3])
    if declared is None:
        raise SignalingError(_OFFER_REJECTION)
    return declared


class DirectSourceWebRTC:
    """Bind one authorized peer to one inbound microphone."""

    def __init__(
        self,
        binding: AuthorizedSessionBinding,
        peer: WebRTCPeer | None = None,
        *,
        config: TransportQueueConfig | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._binding = binding
        self._peer = peer if peer is not None else SmallWebRTCPeer()
        self._config = config or TransportQueueConfig()
        self._now = now or _utc_now
        self._source_id = direct_source_id(binding.peer_id)
        self._queue = InboundQueue(
            self._config,
            clock or _monotonic_ms,
            tenant_id=binding.tenant_id,
            session_id=binding.session_id,
            peer_id=binding.peer_id,
            source_id=self._source_id,
        )
        self._condition = asyncio.Condition()
        self._state_lock = asyncio.Lock()
        self._state: PeerState | None = None
        self._busy = False
        self._source_open = False
        self._reader: asyncio.Task[None] | None = None
        self._shutdown_future: asyncio.Future[None] | None = None
        self._closed = asyncio.Event()
        self._peer_closed = False

    @property
    def state(self) -> PeerState | None:
        """``None`` until signaling has connected the peer."""
        return self._state

    @property
    def source_id(self) -> str:
        """The one microphone this peer accepts, ``{peer_id}/microphone``."""
        return self._source_id

    async def accept_offer(self, sdp: str) -> str:
        """Complete signaling for one sending microphone and start reading it."""
        async with self._state_lock:
            if self._busy or self._shutdown_future is not None or self._state is not None:
                self._reject_log("peer is not accepting signaling")
                raise TransportError("peer is not accepting signaling")
            self._busy = True
        try:
            if self._now() >= self._binding.expires_at:
                await self._reject("join credential expired")
            try:
                audio_format = direct_microphone_format(sdp)
            except SignalingError as error:
                await self._reject(str(error))
            answer = await self._negotiate(sdp)
            await self._activate(audio_format)
            return answer
        finally:
            async with self._state_lock:
                self._busy = False

    def events(self) -> AsyncIterator[TransportEvent]:
        """Yield queued media and lifecycle events until the peer is closed."""
        return self._events()

    async def close(self, reason: CloseReason = CloseReason.SERVER_CLOSE) -> None:
        """Stop the peer. Frames already queued are delivered before removal."""
        await self._shutdown(reason)

    async def wait_closed(self) -> None:
        """Wait until the peer has finished closing."""
        await self._closed.wait()

    async def _events(self) -> AsyncIterator[TransportEvent]:
        while True:
            event, drops, finished = await self._pull()
            for drop in drops:
                _log_drop(
                    self._binding,
                    self._source_id,
                    self._config,
                    drop,
                    drop.queued_frames,
                )
            if isinstance(event, OrderedFrame):
                frame = event.frame
                logger.debug(
                    "audio frame accepted",
                    tenant_id=self._binding.tenant_id,
                    session_id=self._binding.session_id,
                    peer_id=self._binding.peer_id,
                    source_id=frame.source_id,
                    sequence=frame.sequence,
                    timestamp_ms=frame.timestamp_ms,
                    byte_count=len(frame.payload),
                    discontinuity=frame.discontinuity,
                    gap_ms=frame.gap_ms,
                )
            if event is not None:
                yield event
            if finished:
                return

    async def _pull(self) -> tuple[TransportEvent | None, tuple[Drop, ...], bool]:
        async with self._condition:
            while True:
                event, drops = self._queue.pop()
                if event is None and not drops:
                    if self._queue.sealed:
                        return None, (), True
                    await self._condition.wait()
                    continue
                return event, drops, event is None and self._queue.sealed

    async def _activate(self, audio_format: AudioFormat) -> None:
        async with self._state_lock:
            if self._shutdown_future is not None:
                raise TransportError("peer is not accepting signaling")
            self._state = PeerState.CONNECTED
            self._source_open = True
            async with self._condition:
                self._queue.add_source(
                    SourceAdded(
                        tenant_id=self._binding.tenant_id,
                        session_id=self._binding.session_id,
                        peer_id=self._binding.peer_id,
                        source_id=self._source_id,
                        codec=audio_format.codec,
                        sample_rate=audio_format.sample_rate,
                        channel_count=audio_format.channel_count,
                    )
                )
                self._condition.notify_all()
            self._reader = asyncio.create_task(
                self._receive(),
                name=f"webrtc-{self._binding.peer_id}",
            )
        logger.info(
            "peer connected",
            tenant_id=self._binding.tenant_id,
            session_id=self._binding.session_id,
            peer_id=self._binding.peer_id,
        )
        logger.info(
            "source added",
            tenant_id=self._binding.tenant_id,
            session_id=self._binding.session_id,
            peer_id=self._binding.peer_id,
            source_id=self._source_id,
            codec=audio_format.codec,
            sample_rate=audio_format.sample_rate,
            channel_count=audio_format.channel_count,
        )

    async def _negotiate(self, sdp: str) -> str:
        try:
            answer = await self._peer.negotiate(sdp)
        except Exception as error:
            logger.exception(
                "peer media failed",
                tenant_id=self._binding.tenant_id,
                session_id=self._binding.session_id,
                peer_id=self._binding.peer_id,
            )
            await self._shutdown(CloseReason.SIGNALING_REJECTED)
            raise SignalingError("signaling failed") from error
        if not answer or not answer.strip():
            await self._reject("signaling produced no answer")
        return answer

    async def _reject(self, message: str) -> NoReturn:
        self._reject_log(message)
        await self._shutdown(CloseReason.SIGNALING_REJECTED)
        raise SignalingError(message)

    def _reject_log(self, message: str) -> None:
        logger.error(
            "signaling rejected",
            tenant_id=self._binding.tenant_id,
            session_id=self._binding.session_id,
            peer_id=self._binding.peer_id,
            error=message,
        )

    async def _receive(self) -> None:
        try:
            async for audio in self._peer.frames():
                async with self._condition:
                    result = self._queue.push(audio)
                    self._condition.notify_all()
                if result is None:
                    return
                for drop in result.drops:
                    _log_drop(
                        self._binding,
                        self._source_id,
                        self._config,
                        drop,
                        result.queued_frames,
                    )
        except asyncio.CancelledError:
            raise
        except SourceDisconnected as disconnected:
            if disconnected.reason is not CloseReason.CLIENT_DISCONNECT:
                logger.error(
                    "peer media failed",
                    tenant_id=self._binding.tenant_id,
                    session_id=self._binding.session_id,
                    peer_id=self._binding.peer_id,
                    source_id=self._source_id,
                    error=disconnected.detail,
                )
            await self._shutdown(disconnected.reason)
        except Exception:
            logger.exception(
                "peer media failed",
                tenant_id=self._binding.tenant_id,
                session_id=self._binding.session_id,
                peer_id=self._binding.peer_id,
                source_id=self._source_id,
            )
            await self._shutdown(CloseReason.MEDIA_FAILED)
        else:
            await self._end_source()

    async def _end_source(self) -> None:
        async with self._state_lock:
            if not self._source_open or self._shutdown_future is not None:
                return
            self._source_open = False
            async with self._condition:
                self._queue.remove_source(
                    SourceRemoved(
                        tenant_id=self._binding.tenant_id,
                        session_id=self._binding.session_id,
                        peer_id=self._binding.peer_id,
                        source_id=self._source_id,
                        reason=CloseReason.SOURCE_ENDED,
                    )
                )
                self._condition.notify_all()
        logger.info(
            "source removed",
            tenant_id=self._binding.tenant_id,
            session_id=self._binding.session_id,
            peer_id=self._binding.peer_id,
            source_id=self._source_id,
            reason=CloseReason.SOURCE_ENDED.value,
        )

    async def _shutdown(self, reason: CloseReason) -> None:
        async with self._state_lock:
            if self._shutdown_future is not None:
                waiter = self._shutdown_future
                owner = False
            else:
                waiter = asyncio.get_running_loop().create_future()
                self._shutdown_future = waiter
                owner = True
                reader = self._reader
                self._reader = None
                source_open = self._source_open
                self._source_open = False
                self._state = PeerState.CLOSING
        if not owner:
            await waiter
            return
        try:
            if reader is not None and reader is not asyncio.current_task():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            await self._close_peer()
            removed = None
            if source_open:
                removed = SourceRemoved(
                    tenant_id=self._binding.tenant_id,
                    session_id=self._binding.session_id,
                    peer_id=self._binding.peer_id,
                    source_id=self._source_id,
                    reason=reason,
                )
                logger.info(
                    "source removed",
                    tenant_id=self._binding.tenant_id,
                    session_id=self._binding.session_id,
                    peer_id=self._binding.peer_id,
                    source_id=self._source_id,
                    reason=reason.value,
                )
            async with self._condition:
                self._queue.close_peer(
                    removed,
                    PeerClosed(
                        tenant_id=self._binding.tenant_id,
                        session_id=self._binding.session_id,
                        peer_id=self._binding.peer_id,
                        reason=reason,
                        state=PeerState.CLOSED,
                    ),
                )
                self._condition.notify_all()
            self._state = PeerState.CLOSED
            logger.info(
                "peer closed",
                tenant_id=self._binding.tenant_id,
                session_id=self._binding.session_id,
                peer_id=self._binding.peer_id,
                reason=reason.value,
            )
        finally:
            if not waiter.done():
                waiter.set_result(None)
            self._closed.set()

    async def _close_peer(self) -> None:
        if self._peer_closed:
            return
        self._peer_closed = True
        try:
            await self._peer.close()
        except Exception:
            logger.exception(
                "peer media failed",
                tenant_id=self._binding.tenant_id,
                session_id=self._binding.session_id,
                peer_id=self._binding.peer_id,
            )


def _log_drop(
    binding: AuthorizedSessionBinding,
    source_id: str,
    config: TransportQueueConfig,
    drop: Drop,
    queued_frames: int,
) -> None:
    message = (
        "audio frame rejected"
        if drop.reason in {"invalid_frame", "frame_too_large"}
        else "transport queue pressure"
    )
    logger.warning(
        message,
        tenant_id=binding.tenant_id,
        session_id=binding.session_id,
        peer_id=binding.peer_id,
        source_id=source_id,
        dropped_sequence=drop.sequence,
        queued_frames=queued_frames,
        max_queued_frames=config.max_queued_frames,
        reason=drop.reason,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _sdp_lines(sdp: str) -> list[str]:
    normalized = sdp.replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def _direction(lines: list[str]) -> str | None:
    found: str | None = None
    for line in lines:
        if line.startswith("a=") and line[2:] in _DIRECTIONS:
            found = line[2:]
    return found


def _rtpmap(lines: list[str], payload: str) -> AudioFormat | None:
    prefix = f"a=rtpmap:{payload} "
    declared: str | None = None
    for line in lines:
        if line.startswith("m="):
            break
        if line.startswith(prefix):
            declared = line[len(prefix) :]
            break
    if declared is None:
        return None
    codec, separator, rest = declared.partition("/")
    if not codec or not separator or not rest:
        return None
    rate_text, channel_separator, channel_text = rest.partition("/")
    try:
        sample_rate = int(rate_text)
        channel_count = int(channel_text) if channel_separator and channel_text else 1
    except ValueError:
        return None
    if sample_rate < 1 or channel_count < 1:
        return None
    return AudioFormat(codec.lower(), sample_rate, channel_count)


class SmallWebRTCPeer:
    """One Pipecat Small WebRTC connection for an inbound microphone.

    ``SmallWebRTCConnection`` answers the offer and owns the audio track.
    This class reads that track into CEHO frames. It does not send audio.
    """

    def __init__(
        self,
        connection: SmallWebRTCConnection | None = None,
        *,
        ice_servers: list[RTCIceServer] | None = None,
    ) -> None:
        if connection is None:
            connection = SmallWebRTCConnection(ice_servers=ice_servers)
        self._connection = connection
        self._local_close = False
        self._failure: CloseReason | None = None
        self._detail = "media failed"
        self._stopped = asyncio.Event()
        self._track: Any = None
        self._connection.add_event_handler("failed", self._on_failed)
        self._connection.add_event_handler("closed", self._on_closed)

    @property
    def ice_servers(self) -> list[RTCIceServer]:
        """STUN and TURN servers this peer gathers against."""
        return list(self._connection.ice_servers)

    async def negotiate(self, offer_sdp: str) -> str:
        await self._connection.initialize(sdp=offer_sdp, type="offer")
        kinds = [transceiver.kind for transceiver in self._connection.pc.getTransceivers()]
        if kinds != ["audio"]:
            raise SignalingError(_OFFER_REJECTION)
        answer = self._connection.get_answer()
        sdp = "" if not answer else str(answer.get("sdp", ""))
        if not sdp.strip():
            raise SignalingError("signaling produced no answer")
        return sdp

    def frames(self) -> AsyncIterator[InboundAudio]:
        return self._frames()

    async def close(self) -> None:
        self._local_close = True
        await self._connection.disconnect()

    async def _on_failed(self, *_args: object) -> None:
        self._fail(CloseReason.MEDIA_FAILED, "ice connection failed")

    async def _on_closed(self, *_args: object) -> None:
        if not self._local_close:
            self._fail(CloseReason.CLIENT_DISCONNECT, "peer connection closed")

    def _fail(self, reason: CloseReason, detail: str) -> None:
        if self._failure is not None:
            return
        self._failure = reason
        self._detail = detail
        track = self._track
        if track is not None:
            track.stop()
        self._stopped.set()

    async def _frames(self) -> AsyncIterator[InboundAudio]:
        from aiortc.mediastreams import MediaStreamError

        await self._connection.connect()
        track = self._connection.audio_input_track()
        self._track = track
        if track is None or self._failure is not None:
            detail = self._detail if self._failure is not None else "no audio track"
            raise SourceDisconnected(self._failure or CloseReason.MEDIA_FAILED, detail=detail)
        while True:
            if self._failure is not None:
                raise SourceDisconnected(self._failure, detail=self._detail)
            recv_task = asyncio.create_task(track.recv())
            stop_task = asyncio.create_task(self._stopped.wait())
            try:
                done, _pending = await asyncio.wait(
                    {recv_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (recv_task, stop_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(recv_task, stop_task, return_exceptions=True)
            if self._failure is not None:
                raise SourceDisconnected(self._failure, detail=self._detail)
            if recv_task not in done:
                continue
            try:
                frame = recv_task.result()
            except MediaStreamError:
                return
            if frame is None:
                continue
            yield _pcm_frame(frame)


def ice_servers_for(relay: IceRelay) -> list[RTCIceServer]:
    """STUN, plus TURN when this relay carries a username and password.

    The TURN entry carries the short-lived username and password. The answer
    gathers against these servers, so it can include a server-reflexive address
    or a relay candidate. A relay minted without Cloudflare credentials contributes
    only the STUN server.
    """
    servers = [RTCIceServer(urls=relay.stun_url)]
    if relay.turn_url and relay.username and relay.password:
        servers.append(
            RTCIceServer(urls=relay.turn_url, username=relay.username, credential=relay.password)
        )
    return servers


def _pcm_frame(frame: Any) -> InboundAudio:
    sample_rate = int(frame.sample_rate)
    samples = int(frame.samples)
    duration_ms = samples * 1000 // sample_rate if sample_rate else 0
    time_base = frame.time_base
    pts = frame.pts
    if pts is None or time_base is None:
        timestamp_ms = 0
    else:
        timestamp_ms = int(float(pts * time_base) * 1000)
    channels = len(frame.layout.channels) or 1
    formatted = frame if frame.format.name == "s16" else frame.reformat(format="s16")
    payload = bytes(formatted.planes[0])
    if duration_ms < 1:
        duration_ms = 1
    return InboundAudio(
        timestamp_ms=timestamp_ms,
        duration_ms=duration_ms,
        payload=payload,
        codec="pcm_s16le",
        sample_rate=sample_rate,
        channel_count=channels,
    )
