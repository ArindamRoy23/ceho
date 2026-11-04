"""Durable recording for one direct audio source."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from ceho.hot.config import (
    ConfigurationError,
    DirectSourcePipelineConfig,
    HotServerSettings,
    RecordingConfig,
)
from ceho.hot.media import (
    S3_MIN_PART_BYTES,
    DatabaseMediaCatalog,
    DirectSourceRecording,
    ObjectUpload,
    RecordingError,
    S3ObjectStorage,
    UploadedPart,
    s3_object_storage,
)
from ceho.hot.session import FinalizeTranscript, finalize_transcript
from ceho.models import MediaObject, Transcript
from tests.conftest import ActiveSession, as_table
from tests.hot.test_config import _environment

_STARTED = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
_ONE_BYTE_PER_MS = DirectSourcePipelineConfig(sample_rate=1000, sample_width=1, channels=1)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeStorage:
    """Multipart store that keeps parts in memory."""

    def __init__(self, *, part_failures: int = 0, fail_start: bool = False) -> None:
        self.part_failures = part_failures
        self.fail_start = fail_start
        self.keys: list[str] = []
        self.content_types: list[str] = []
        self.parts: list[tuple[str, int, bytes]] = []
        self.completed: list[tuple[str, Sequence[UploadedPart]]] = []
        self.aborted: list[str] = []
        self.block = asyncio.Event()
        self.block.set()
        self.entered = asyncio.Event()

    def object_uri(self, key: str) -> str:
        return f"s3://test-bucket/{key}"

    async def start_upload(self, key: str, content_type: str) -> ObjectUpload:
        if self.fail_start:
            raise RuntimeError("start failed")
        self.keys.append(key)
        self.content_types.append(content_type)
        return ObjectUpload(key=key, upload_id="upload-1")

    async def upload_part(
        self, upload: ObjectUpload, part_number: int, body: bytes
    ) -> UploadedPart:
        self.entered.set()
        await self.block.wait()
        if self.part_failures > 0:
            self.part_failures -= 1
            raise RuntimeError("upload failed")
        self.parts.append((upload.key, part_number, body))
        return UploadedPart(part_number=part_number, etag=f"etag-{part_number}")

    async def complete_upload(self, upload: ObjectUpload, parts: Sequence[UploadedPart]) -> None:
        self.completed.append((upload.key, parts))

    async def abort_upload(self, upload: ObjectUpload) -> None:
        self.aborted.append(upload.upload_id)
        self.block.set()


class FakeCatalog:
    """Media-object rows kept in memory."""

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[int] = []
        self._next = 1

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
        media_id = self._next
        self._next += 1
        self.opened.append(
            {
                "media_object_id": media_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "storage_uri": storage_uri,
                "content_type": content_type,
                "codec": codec,
                "sample_rate_hz": sample_rate_hz,
                "channel_count": channel_count,
            }
        )
        return media_id

    def complete_media(
        self,
        tenant_id: int,
        session_id: int,
        media_object_id: int,
        media_started_at: datetime,
        media_ended_at: datetime,
        byte_length: int,
        checksum: str,
    ) -> None:
        self.completed.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "media_object_id": media_object_id,
                "media_started_at": media_started_at,
                "media_ended_at": media_ended_at,
                "byte_length": byte_length,
                "checksum": checksum,
            }
        )

    def fail_media(self, tenant_id: int, session_id: int, media_object_id: int) -> None:
        self.failed.append(media_object_id)


def _recording(
    storage: FakeStorage,
    catalog: FakeCatalog,
    *,
    config: RecordingConfig | None = None,
    audio: DirectSourcePipelineConfig = _ONE_BYTE_PER_MS,
    clock: Callable[[], float] | None = None,
    source_id: str = "mic",
    session_started_at: datetime = _STARTED,
) -> DirectSourceRecording:
    return DirectSourceRecording(
        tenant_id=1,
        session_id=10,
        source_id=source_id,
        session_started_at=session_started_at,
        storage=storage,
        catalog=catalog,
        config=config or RecordingConfig(part_bytes=8),
        audio=audio,
        clock=clock,
    )


def _sha256(pcm: bytes) -> str:
    return "sha256:" + hashlib.sha256(pcm).hexdigest()


async def test_recording_uploads_pcm_under_a_server_owned_s3_key() -> None:
    storage = FakeStorage()
    catalog = FakeCatalog()
    recording = _recording(storage, catalog)

    await recording.accept(b"abcdef", timestamp_ms=0)
    await recording.finish()

    key = storage.keys[0]
    tenant, session, source, filename = key.split("/")
    assert tenant == "1"
    assert session == "10"
    assert source == "mic"
    assert filename.endswith(".pcm")
    UUID(filename.removesuffix(".pcm"))
    assert catalog.opened[0]["storage_uri"] == f"s3://test-bucket/{key}"
    assert storage.content_types == ["audio/L16"]
    assert storage.parts[0][2] == b"abcdef"
    assert storage.aborted == []


async def test_a_second_attachment_opens_a_different_object() -> None:
    first = FakeStorage()
    second = FakeStorage()
    catalog = FakeCatalog()

    opened = _recording(first, catalog)
    other = _recording(second, catalog)
    await opened.accept(b"aa", timestamp_ms=0)
    await other.accept(b"bb", timestamp_ms=0)
    await opened.finish()
    await other.finish()

    assert first.keys[0] != second.keys[0]


async def test_short_recording_completes_as_one_final_part() -> None:
    storage = FakeStorage()
    catalog = FakeCatalog()
    recording = _recording(storage, catalog, config=RecordingConfig(part_bytes=100))

    await recording.accept(b"short", timestamp_ms=0)
    await recording.finish()

    assert [body for _, _, body in storage.parts] == [b"short"]
    assert [part.part_number for part in storage.completed[0][1]] == [1]


async def test_recording_rolls_a_part_at_the_configured_size() -> None:
    storage = FakeStorage()
    catalog = FakeCatalog()
    recording = _recording(storage, catalog, config=RecordingConfig(part_bytes=4))

    await recording.accept(b"abcd", timestamp_ms=0)
    await recording.accept(b"ef", timestamp_ms=4)
    await recording.finish()

    assert [body for _, _, body in storage.parts] == [b"abcd", b"ef"]
    assert [part.part_number for part in storage.completed[0][1]] == [1, 2]


async def test_empty_source_persists_nothing() -> None:
    storage = FakeStorage()
    catalog = FakeCatalog()
    recording = _recording(storage, catalog)

    await recording.finish()

    assert storage.keys == []
    assert catalog.opened == []
    assert catalog.completed == []


async def test_completion_records_codec_timeline_checksum_and_byte_count() -> None:
    storage = FakeStorage()
    catalog = FakeCatalog()
    recording = _recording(storage, catalog)
    pcm = b"hello"

    await recording.accept(pcm, timestamp_ms=1_000)
    await recording.finish()

    opened = catalog.opened[0]
    completed = catalog.completed[0]
    assert opened["codec"] == "pcm_s16le"
    assert opened["content_type"] == "audio/L16"
    assert opened["sample_rate_hz"] == 1000
    assert opened["channel_count"] == 1
    assert completed["byte_length"] == len(pcm)
    assert completed["checksum"] == _sha256(pcm)
    assert completed["media_started_at"] == _STARTED + timedelta(milliseconds=1_000)
    assert completed["media_ended_at"] == _STARTED + timedelta(milliseconds=1_005)


async def test_storage_error_marks_the_media_object_failed_and_aborts() -> None:
    storage = FakeStorage(part_failures=2)
    catalog = FakeCatalog()
    recording = _recording(storage, catalog, config=RecordingConfig(part_bytes=100))

    await recording.accept(b"audio", timestamp_ms=0)
    with pytest.raises(RecordingError, match="upload failed"):
        await recording.finish()

    assert catalog.failed == [catalog.opened[0]["media_object_id"]]
    assert storage.aborted == ["upload-1"]
    assert storage.completed == []


async def test_start_failure_marks_the_row_failed_without_an_abort() -> None:
    storage = FakeStorage(fail_start=True)
    catalog = FakeCatalog()
    recording = _recording(storage, catalog)

    with pytest.raises(RecordingError, match="start failed"):
        await recording.accept(b"audio", timestamp_ms=0)

    assert catalog.failed == [catalog.opened[0]["media_object_id"]]
    assert storage.aborted == []
    assert storage.completed == []


async def test_part_upload_retries_once_then_completes() -> None:
    storage = FakeStorage(part_failures=1)
    catalog = FakeCatalog()
    recording = _recording(storage, catalog, config=RecordingConfig(part_bytes=100))

    await recording.accept(b"audio", timestamp_ms=0)
    await recording.finish()

    assert [body for _, _, body in storage.parts] == [b"audio"]
    assert catalog.completed[0]["checksum"] == _sha256(b"audio")
    assert storage.aborted == []


async def test_queue_overflow_stops_without_growing_past_the_bound() -> None:
    storage = FakeStorage()
    storage.block.clear()
    catalog = FakeCatalog()
    recording = _recording(
        storage,
        catalog,
        config=RecordingConfig(max_queued_bytes=30, max_queued_age_ms=15_000, part_bytes=10),
    )

    await recording.accept(b"a" * 10, timestamp_ms=0)
    await asyncio.wait_for(storage.entered.wait(), timeout=1)
    await recording.accept(b"b" * 10, timestamp_ms=10)
    await recording.accept(b"c" * 10, timestamp_ms=20)
    await recording.accept(b"d" * 10, timestamp_ms=30)

    with pytest.raises(RecordingError, match="queue is full"):
        await asyncio.wait_for(recording.accept(b"e", timestamp_ms=40), timeout=1)

    assert catalog.failed == [catalog.opened[0]["media_object_id"]]
    storage.block.set()
    with pytest.raises(RecordingError, match="queue is full"):
        await asyncio.wait_for(recording.finish(), timeout=1)
    assert storage.completed == []
    assert b"e" not in {body for _, _, body in storage.parts}


async def test_stale_queued_frame_stops_the_recording() -> None:
    storage = FakeStorage()
    storage.block.clear()
    clock = _Clock()
    catalog = FakeCatalog()
    recording = _recording(
        storage,
        catalog,
        config=RecordingConfig(max_queued_bytes=1_000, max_queued_age_ms=15_000, part_bytes=5),
        clock=clock,
    )

    await recording.accept(b"12345", timestamp_ms=0)
    await asyncio.wait_for(storage.entered.wait(), timeout=1)
    await recording.accept(b"abc", timestamp_ms=5)
    clock.now = 16

    with pytest.raises(RecordingError, match="stale"):
        await asyncio.wait_for(recording.accept(b"z", timestamp_ms=8), timeout=1)

    storage.block.set()
    with pytest.raises(RecordingError):
        await asyncio.wait_for(recording.finish(), timeout=1)
    assert catalog.failed


async def test_provenance_offsets_match_the_session_timeline() -> None:
    storage = FakeStorage()
    catalog = FakeCatalog()
    recording = _recording(storage, catalog, config=RecordingConfig(part_bytes=100))

    await recording.accept(b"w" * 700, timestamp_ms=1_000)
    provenance = recording.provenance(1_500, 1_700)
    await recording.finish()

    assert provenance.media_object_id == catalog.opened[0]["media_object_id"]
    assert provenance.media_start_offset_ms == 500
    assert provenance.media_end_offset_ms == 700


async def test_provenance_rejects_an_interval_outside_the_recording() -> None:
    storage = FakeStorage()
    catalog = FakeCatalog()
    recording = _recording(storage, catalog)

    with pytest.raises(RecordingError, match="no media object"):
        recording.provenance(0, 1)
    await recording.accept(b"w" * 10, timestamp_ms=1_000)

    with pytest.raises(RecordingError, match="outside the recording"):
        recording.provenance(999, 1_001)
    with pytest.raises(RecordingError, match="outside the recording"):
        recording.provenance(1_000, 1_011)
    await recording.finish()


async def test_recording_failure_is_logged_without_audio_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="ceho.hot.media")
    storage = FakeStorage(part_failures=2)
    catalog = FakeCatalog()
    recording = _recording(storage, catalog, config=RecordingConfig(part_bytes=100))
    secret = b"PCM-SECRET-BYTES"

    await recording.accept(secret, timestamp_ms=0)
    with pytest.raises(RecordingError):
        await recording.finish()

    failed = [record for record in caplog.records if record.msg == "recording failed"]
    retried = [record for record in caplog.records if record.msg == "recording part upload retry"]
    assert len(failed) == 1
    assert failed[0].levelname == "ERROR"
    assert len(retried) == 1
    assert retried[0].levelname == "WARNING"
    fields = getattr(failed[0], "ceho_fields", None)
    assert isinstance(fields, dict)
    assert fields["tenant_id"] == 1
    assert fields["session_id"] == 10
    assert fields["source_id"] == "mic"
    assert fields["media_id"] == catalog.opened[0]["media_object_id"]
    rendered = " ".join(
        str(record.msg) + str(getattr(record, "ceho_fields", "")) for record in caplog.records
    )
    assert b"PCM-SECRET-BYTES" not in rendered.encode()
    assert "PCM-SECRET-BYTES" not in rendered


async def test_source_id_must_be_a_single_path_segment() -> None:
    with pytest.raises(RecordingError, match="source_id"):
        _recording(FakeStorage(), FakeCatalog(), source_id="mic/room")


def test_s3_storage_rejects_a_part_smaller_than_five_mebibytes() -> None:
    settings = HotServerSettings.from_environment(_environment())

    with pytest.raises(ConfigurationError, match=str(S3_MIN_PART_BYTES)):
        s3_object_storage(settings, RecordingConfig(part_bytes=1024))


def test_s3_object_uri_uses_the_configured_bucket() -> None:
    storage = S3ObjectStorage(object(), bucket="ceho-media", prefix="")

    assert storage.object_uri("1/10/mic/file.pcm") == "s3://ceho-media/1/10/mic/file.pcm"


async def test_database_row_completes_and_provenance_can_be_finalized(
    active_session: ActiveSession,
) -> None:
    storage = FakeStorage()
    catalog = DatabaseMediaCatalog(active_session.engine)
    pcm = b"\x00\x01" * 32
    recording = DirectSourceRecording(
        tenant_id=active_session.tenant_id,
        session_id=active_session.session_id,
        source_id="mic",
        session_started_at=active_session.started_at,
        storage=storage,
        catalog=catalog,
        config=RecordingConfig(part_bytes=1024),
        audio=DirectSourcePipelineConfig(),
    )

    await recording.accept(pcm, timestamp_ms=1_000)
    provenance = recording.provenance(1_000, 1_002)
    with active_session.connect() as connection:
        finalize_transcript(
            connection,
            FinalizeTranscript(
                tenant_id=active_session.tenant_id,
                session_id=active_session.session_id,
                text="hello",
                start_ms=1_000,
                end_ms=1_002,
                media_object_id=provenance.media_object_id,
                media_start_offset_ms=provenance.media_start_offset_ms,
                media_end_offset_ms=provenance.media_end_offset_ms,
            ),
        )
    await recording.finish()

    with active_session.connect() as connection:
        media = (
            connection.execute(
                select(as_table(MediaObject)).where(
                    as_table(MediaObject).c.media_object_id == provenance.media_object_id
                )
            )
            .mappings()
            .one()
        )
        transcript = (
            connection.execute(
                select(as_table(Transcript)).where(
                    as_table(Transcript).c.session_id == active_session.session_id
                )
            )
            .mappings()
            .one()
        )

    assert media["write_status"] == "complete"
    assert media["codec"] == "pcm_s16le"
    assert media["sample_rate_hz"] == 16_000
    assert media["channel_count"] == 1
    assert media["byte_length"] == len(pcm)
    assert media["checksum"] == _sha256(pcm)
    assert media["storage_uri"].startswith("s3://test-bucket/")
    assert media["media_started_at"] == active_session.started_at + timedelta(milliseconds=1_000)
    assert media["media_ended_at"] == active_session.started_at + timedelta(milliseconds=1_002)
    assert transcript["media_object_id"] == provenance.media_object_id
    assert transcript["media_start_offset_ms"] == 0
    assert transcript["media_end_offset_ms"] == 2


async def test_database_failure_is_persisted(
    active_session: ActiveSession,
) -> None:
    storage = FakeStorage(part_failures=2)
    recording = DirectSourceRecording(
        tenant_id=active_session.tenant_id,
        session_id=active_session.session_id,
        source_id="mic",
        session_started_at=active_session.started_at,
        storage=storage,
        catalog=DatabaseMediaCatalog(active_session.engine),
        config=RecordingConfig(part_bytes=100),
        audio=_ONE_BYTE_PER_MS,
    )

    await recording.accept(b"audio", timestamp_ms=0)
    with pytest.raises(RecordingError, match="upload failed"):
        await recording.finish()

    with active_session.connect() as connection:
        media = (
            connection.execute(
                select(as_table(MediaObject)).where(
                    as_table(MediaObject).c.session_id == active_session.session_id
                )
            )
            .mappings()
            .one()
        )
    assert media["write_status"] == "failed"
    assert media["checksum"] is None
    assert storage.aborted == ["upload-1"]
