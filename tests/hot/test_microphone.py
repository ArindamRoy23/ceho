"""Attach the direct-source pipeline and recorder to one connected microphone."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import Frame, InputAudioRawFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ceho.hot.config import DirectSourcePipelineConfig, RecordingConfig
from ceho.hot.media import DirectSourceRecording, MediaProvenance, ObjectUpload, UploadedPart
from ceho.hot.microphone import attach_microphone, direct_source_processors, recording_source_id
from ceho.hot.pipeline import RunState
from ceho.hot.server import create_app
from ceho.hot.transport import (
    AudioFrame,
    CloseReason,
    InboundAudio,
    OrderedFrame,
    PeerClosed,
    PeerState,
    QueuePressure,
    SourceAdded,
    SourceRemoved,
    TransportEvent,
)
from ceho.hot.turn import IceRelay
from tests.hot.conftest import MemoryControlStore, hot_settings
from tests.hot.test_offer import ANSWER, OFFER, _session, _signaling, _Turn
from tests.hot.test_session_api import NOW

_STARTED = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
_PHRASE = "secret phrase"


class _Transport:
    """Scripted direct-source transport."""

    def __init__(self, events: Sequence[TransportEvent]) -> None:
        self._events = list(events)
        self.closed: list[CloseReason] = []

    def events(self) -> AsyncIterator[TransportEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[TransportEvent]:
        for event in self._events:
            yield event

    async def close(self, reason: CloseReason = CloseReason.SERVER_CLOSE) -> None:
        self.closed.append(reason)


class _MemoryRecorder:
    def __init__(self, *, fail: bool = False, accepted: threading.Event | None = None) -> None:
        self.fail = fail
        self.accepted = accepted
        self.chunks: list[tuple[bytes, int]] = []
        self.finished = 0

    async def accept(self, pcm: bytes, *, timestamp_ms: int) -> None:
        if self.fail:
            raise RuntimeError("recording failed")
        self.chunks.append((pcm, timestamp_ms))
        if self.accepted is not None:
            self.accepted.set()

    async def finish(self) -> None:
        self.finished += 1

    def provenance(self, start_ms: int, end_ms: int) -> MediaProvenance:
        return MediaProvenance(1, start_ms, end_ms)


class _Catalog:
    def __init__(self) -> None:
        self.opened: list[dict[str, object]] = []
        self.completed: list[dict[str, object]] = []
        self.failed: list[int] = []

    def open_media(
        self,
        tenant_id: int,
        session_id: int,
        storage_uri: str,
        content_type: str,
        codec: str,
        sample_rate_hz: int,
        channel_count: int,
    ) -> int:
        media_id = len(self.opened) + 1
        self.opened.append(
            {
                "media_object_id": media_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "storage_uri": storage_uri,
                "sample_rate_hz": sample_rate_hz,
                "channel_count": channel_count,
                "content_type": content_type,
                "codec": codec,
            }
        )
        return media_id

    def complete_media(self, *_args: object, **_kwargs: object) -> None:
        self.completed.append({"args": _args, "kwargs": _kwargs})

    def fail_media(self, tenant_id: int, session_id: int, media_object_id: int) -> None:
        del tenant_id, session_id
        self.failed.append(media_object_id)


class _Storage:
    def __init__(self) -> None:
        self.completed = 0
        self.aborted = 0

    def object_uri(self, key: str) -> str:
        return f"s3://test-bucket/{key}"

    async def start_upload(self, key: str, content_type: str) -> ObjectUpload:
        del content_type
        return ObjectUpload(key=key, upload_id="upload-1")

    async def upload_part(
        self, upload: ObjectUpload, part_number: int, body: bytes
    ) -> UploadedPart:
        del upload, body
        return UploadedPart(part_number=part_number, etag=f"etag-{part_number}")

    async def complete_upload(self, upload: ObjectUpload, parts: Sequence[UploadedPart]) -> None:
        del upload, parts
        self.completed += 1

    async def abort_upload(self, upload: ObjectUpload) -> None:
        del upload
        self.aborted += 1


class _Result:
    def __init__(self, start: float, duration: float) -> None:
        self.start = start
        self.duration = duration
        self.channel = None


class ScriptedSTT(FrameProcessor):
    """Emit fixed transcriptions the first time audio arrives."""

    def __init__(self, texts: list[tuple[str, float, float]], *, errors: int = 0) -> None:
        super().__init__()
        self._texts = texts
        self._errors = errors
        self._done = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and not self._done:
            self._done = True
            for _ in range(self._errors):
                await self.push_error("provider unavailable")
            for text, start, duration in self._texts:
                await self.push_frame(
                    TranscriptionFrame(
                        text,
                        "user",
                        "2026-01-01T00:00:00Z",
                        result=_Result(start, duration),
                    ),
                    direction,
                )
        await self.push_frame(frame, direction)


class _Commit:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int | None, MediaProvenance | None]] = []
        self._next = 7

    def __call__(
        self,
        candidate: object,
        *,
        session_speaker_id: int | None,
        provenance: MediaProvenance | None,
    ) -> int:
        if self.fail:
            raise RuntimeError("commit failed")
        text = getattr(candidate, "text")
        self.calls.append((text, session_speaker_id, provenance))
        if session_speaker_id is not None:
            return session_speaker_id
        speaker = self._next
        self._next += 1
        return speaker


def _added() -> SourceAdded:
    return SourceAdded(
        tenant_id=4,
        session_id=10,
        peer_id="peer",
        source_id="peer/microphone",
        codec="opus",
        sample_rate=48_000,
        channel_count=2,
    )


def _frame(
    payload: bytes,
    *,
    timestamp_ms: int,
    duration_ms: int = 20,
    sample_rate: int = 16_000,
    channel_count: int = 1,
    discontinuity: bool = False,
    gap_ms: int = 0,
    sequence: int = 1,
    codec: str = "pcm_s16le",
) -> OrderedFrame:
    return OrderedFrame(
        tenant_id=4,
        session_id=10,
        peer_id="peer",
        frame=AudioFrame(
            source_id="peer/microphone",
            sequence=sequence,
            timestamp_ms=timestamp_ms,
            duration_ms=duration_ms,
            codec=codec,
            sample_rate=sample_rate,
            channel_count=channel_count,
            payload=payload,
            discontinuity=discontinuity,
            gap_ms=gap_ms,
        ),
    )


def _removed(reason: CloseReason = CloseReason.SOURCE_ENDED) -> SourceRemoved:
    return SourceRemoved(
        tenant_id=4,
        session_id=10,
        peer_id="peer",
        source_id="peer/microphone",
        reason=reason,
    )


def _closed(reason: CloseReason = CloseReason.CLIENT_DISCONNECT) -> PeerClosed:
    return PeerClosed(
        tenant_id=4,
        session_id=10,
        peer_id="peer",
        reason=reason,
        state=PeerState.CLOSED,
    )


def _pressure() -> QueuePressure:
    return QueuePressure(
        tenant_id=4,
        session_id=10,
        peer_id="peer",
        source_id="peer/microphone",
        dropped_sequence=2,
        dropped_frames=1,
        queued_frames=1,
        max_queued_frames=50,
        reason="queue_full",
    )


def _pcm16(sample: int, count: int) -> bytes:
    return sample.to_bytes(2, "little", signed=True) * count


def _stereo(sample: int, count: int) -> bytes:
    frame = sample.to_bytes(2, "little", signed=True) * 2
    return frame * count


async def test_final_transcript_reuses_the_session_speaker_and_stores_one_object() -> None:
    pcm = _pcm16(1000, 16_000)
    storage = _Storage()
    catalog = _Catalog()
    recorder = DirectSourceRecording(
        tenant_id=4,
        session_id=10,
        source_id=recording_source_id("peer/microphone"),
        session_started_at=_STARTED,
        storage=storage,
        catalog=catalog,
        config=RecordingConfig(part_bytes=8),
    )
    commit = _Commit()
    transport = _Transport(
        [
            _added(),
            _frame(pcm, timestamp_ms=1000, duration_ms=1000),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[
            ScriptedSTT(
                [
                    (_PHRASE, 0.0, 0.1),
                    ("second turn", 0.2, 0.2),
                ]
            )
        ],
        recorder=recorder,
        commit=commit,
    )

    assert stopped is not None
    assert stopped.ends_session is False
    assert stopped.state is RunState.STOPPED
    assert len(catalog.opened) == 1
    assert catalog.opened[0]["sample_rate_hz"] == 16_000
    assert catalog.opened[0]["channel_count"] == 1
    assert len(catalog.completed) == 1
    assert storage.completed == 1
    assert storage.aborted == 0
    assert [call[0] for call in commit.calls] == [_PHRASE, "second turn"]
    assert commit.calls[0][1] is None
    assert commit.calls[1][1] == 7
    first = commit.calls[0][2]
    assert isinstance(first, MediaProvenance)
    assert first.media_object_id == 1
    assert first.media_start_offset_ms == 0
    assert first.media_end_offset_ms == 100


async def test_timestamp_gap_is_pushed_without_filling_silence() -> None:
    first = _pcm16(300, 320)
    second = _pcm16(400, 320)
    recorder = _MemoryRecorder()
    transport = _Transport(
        [
            _added(),
            _frame(first, timestamp_ms=0, duration_ms=20, sequence=1),
            _frame(
                second,
                timestamp_ms=500,
                duration_ms=20,
                sequence=2,
                discontinuity=True,
                gap_ms=480,
            ),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[],
        recorder=recorder,
        commit=_Commit(),
    )

    assert stopped is not None
    assert stopped.ends_session is False
    assert recorder.chunks == [(first, 0), (second, 500)]
    assert recorder.finished == 1
    assert all(len(pcm) < 15_000 for pcm, _timestamp in recorder.chunks)


async def test_browser_rate_is_resampled_to_pipeline_pcm() -> None:
    payload = _stereo(2000, 4800)
    recorder = _MemoryRecorder()
    transport = _Transport(
        [
            _added(),
            _frame(
                payload,
                timestamp_ms=40,
                duration_ms=100,
                sample_rate=48_000,
                channel_count=2,
            ),
            _closed(CloseReason.SOURCE_ENDED),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[],
        recorder=recorder,
        commit=_Commit(),
    )

    assert stopped is not None
    assert stopped.ends_session is False
    assert len(recorder.chunks) == 1
    pcm, timestamp_ms = recorder.chunks[0]
    assert timestamp_ms == 40
    assert len(pcm) % 2 == 0
    assert 2800 <= len(pcm) <= 3600
    assert pcm != payload
    assert recorder.finished == 1


@pytest.mark.parametrize("terminal", [_removed(), _closed()])
async def test_source_or_peer_close_stops_the_run_without_ending_the_session(
    terminal: SourceRemoved | PeerClosed,
) -> None:
    recorder = _MemoryRecorder()
    session = {"status": "active"}
    transport = _Transport([_added(), _frame(_pcm16(1, 160), timestamp_ms=0), terminal])

    stopped = await attach_microphone(
        transport,
        processors=[],
        recorder=recorder,
        commit=_Commit(),
    )

    assert session["status"] == "active"
    assert stopped is not None
    assert stopped.ends_session is False
    assert stopped.state is RunState.STOPPED
    assert recorder.finished == 1
    assert transport.closed == []


async def test_queue_pressure_is_logged_and_the_run_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="ceho.hot.microphone")
    recorder = _MemoryRecorder()
    pcm = _pcm16(5, 80)
    transport = _Transport(
        [
            _added(),
            _pressure(),
            _frame(pcm, timestamp_ms=15),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[],
        recorder=recorder,
        commit=_Commit(),
    )

    assert stopped is not None
    assert stopped.state is RunState.STOPPED
    assert recorder.chunks == [(pcm, 15)]
    assert any(record.msg == "transport queue pressure" for record in caplog.records)


async def test_recording_failure_cancels_the_run_and_does_not_end_the_session() -> None:
    recorder = _MemoryRecorder(fail=True)
    later = _pcm16(9, 80)
    transport = _Transport(
        [
            _added(),
            _frame(_pcm16(1, 80), timestamp_ms=0, sequence=1),
            _frame(later, timestamp_ms=20, sequence=2),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[],
        recorder=recorder,
        commit=_Commit(),
    )

    assert stopped is not None
    assert stopped.ends_session is False
    assert stopped.state is RunState.FAILED
    assert recorder.chunks == []
    assert recorder.finished == 1
    assert transport.closed == [CloseReason.MEDIA_FAILED]


async def test_one_provider_failure_still_commits_the_final() -> None:
    commit = _Commit()
    recorder = _MemoryRecorder()
    transport = _Transport(
        [
            _added(),
            _frame(_pcm16(1, 320), timestamp_ms=0),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[ScriptedSTT([(_PHRASE, 0.0, 0.02)], errors=1)],
        recorder=recorder,
        commit=commit,
    )

    assert stopped is not None
    assert stopped.ends_session is False
    assert stopped.state is RunState.STOPPED
    assert [call[0] for call in commit.calls] == [_PHRASE]
    assert transport.closed == []


async def test_provider_failure_budget_commits_nothing_and_leaves_the_peer() -> None:
    commit = _Commit()
    config = DirectSourcePipelineConfig()
    transport = _Transport(
        [
            _added(),
            _frame(_pcm16(1, 320), timestamp_ms=0),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[ScriptedSTT([], errors=config.max_provider_failures)],
        recorder=_MemoryRecorder(),
        commit=commit,
        config=config,
    )

    assert commit.calls == []
    assert stopped is not None
    assert stopped.ends_session is False
    assert stopped.state is RunState.FAILED
    assert transport.closed == []


async def test_transcript_text_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    transport = _Transport(
        [
            _added(),
            _frame(_pcm16(1, 320), timestamp_ms=0),
            _removed(),
            _closed(),
        ]
    )

    await attach_microphone(
        transport,
        processors=[ScriptedSTT([(_PHRASE, 0.0, 0.02)])],
        recorder=_MemoryRecorder(),
        commit=_Commit(),
    )

    rendered = " ".join(
        [
            *(record.getMessage() for record in caplog.records),
            *(
                str(value)
                for record in caplog.records
                for value in getattr(record, "ceho_fields", {}).values()
            ),
        ]
    )
    assert _PHRASE not in rendered


def test_recording_source_id_is_one_path_segment() -> None:
    assert recording_source_id("peer/microphone") == "peer-microphone"


async def test_commit_failure_does_not_end_the_session_or_drop_the_recording() -> None:
    recorder = _MemoryRecorder()
    transport = _Transport(
        [
            _added(),
            _frame(_pcm16(1, 320), timestamp_ms=0),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[ScriptedSTT([(_PHRASE, 0.0, 0.02)])],
        recorder=recorder,
        commit=_Commit(fail=True),
    )

    assert stopped is not None
    assert stopped.ends_session is False
    assert recorder.finished == 1
    assert recorder.chunks


def test_missing_deepgram_key_builds_no_processor() -> None:
    assert direct_source_processors(None) == ()
    assert direct_source_processors("  ") == ()


def test_deepgram_processor_builds_without_connecting() -> None:
    assert len(direct_source_processors("test-key")) == 1


def test_attach_module_does_not_import_webrtc() -> None:
    import ast
    from pathlib import Path

    source = Path("ceho/hot/microphone.py").read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert all("webrtc" not in name for name in imported)


class _SpeakingPeer:
    """One microphone that yields a single 16 kHz frame, then waits to close."""

    def __init__(self, relay: IceRelay) -> None:
        self.relay = relay
        self._stop = asyncio.Event()

    async def negotiate(self, offer_sdp: str) -> str:
        del offer_sdp
        return ANSWER

    def frames(self) -> AsyncIterator[InboundAudio]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[InboundAudio]:
        yield InboundAudio(
            timestamp_ms=0,
            duration_ms=20,
            payload=_pcm16(1000, 320),
            codec="pcm_s16le",
            sample_rate=16_000,
            channel_count=1,
        )
        await self._stop.wait()

    async def close(self) -> None:
        self._stop.set()


def test_connected_microphone_records_and_commits_without_ending_the_session() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    accepted = threading.Event()
    recorder = _MemoryRecorder(accepted=accepted)
    commit = _Commit()
    peers: list[_SpeakingPeer] = []

    def peer_factory(relay: IceRelay) -> _SpeakingPeer:
        peer = _SpeakingPeer(relay)
        peers.append(peer)
        return peer

    app = create_app(
        hot_settings(),
        control_store=store,
        now=lambda: NOW,
        turn=_Turn(),
        peer_factory=peer_factory,
    )
    app.state.recorder_factory = lambda *_args: recorder
    app.state.transcript_commit = commit
    app.state.microphone_processors = [ScriptedSTT([(_PHRASE, 0.0, 0.02)])]

    with TestClient(app) as client:
        session_id = _session(client)
        response = client.post(
            f"/sessions/{session_id}/offer",
            headers={"Authorization": f"Bearer {_signaling(session_id=session_id)}"},
            json={"sdp": OFFER},
        )
        assert response.status_code == 200
        assert accepted.wait(2)

    assert recorder.finished == 1
    assert recorder.chunks
    assert [call[0] for call in commit.calls] == [_PHRASE]
    assert commit.calls[0][1] is None
    assert store.sessions[(4, session_id)].status == "active"
    assert app.state.presence.peer_count(4, session_id) == 0
    assert peers[0].relay.stun_url


async def test_a_non_pcm_frame_is_skipped_and_later_audio_is_kept() -> None:
    good = _pcm16(1, 80)
    recorder = _MemoryRecorder()
    transport = _Transport(
        [
            _added(),
            _frame(b"\x00\x01", timestamp_ms=0, codec="opus", sequence=1),
            _frame(good, timestamp_ms=20, sequence=2),
            _removed(),
            _closed(),
        ]
    )

    stopped = await attach_microphone(
        transport,
        processors=[],
        recorder=recorder,
        commit=_Commit(),
    )

    assert stopped is not None
    assert stopped.state is RunState.STOPPED
    assert stopped.ends_session is False
    assert recorder.chunks == [(good, 20)]
