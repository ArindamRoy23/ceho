"""Durable media recording and provenance boundary.

One direct-source attachment becomes one multipart object in shared storage
and one ``media_objects`` row. ``push_audio`` calls ``accept``. ``close``
calls ``finish``. The pipeline does not import this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import Table, insert, update
from sqlalchemy.engine import Engine

from ceho.hot.config import (
    DIRECT_SOURCE_PIPELINE,
    ConfigurationError,
    DirectSourcePipelineConfig,
    HotServerSettings,
    RecordingConfig,
)
from ceho.logging import get_logger
from ceho.models import Base, MediaObject

logger = get_logger(__name__)

CONTENT_TYPE = "audio/L16"
CODEC = "pcm_s16le"
S3_MIN_PART_BYTES = 5 * 1024 * 1024


class RecordingError(RuntimeError):
    """Durable recording failed and the source must stop."""


@dataclass(frozen=True)
class ObjectUpload:
    """One multipart upload owned by the storage adapter."""

    key: str
    upload_id: str


@dataclass(frozen=True)
class UploadedPart:
    """One uploaded part, in stream order."""

    part_number: int
    etag: str


@dataclass(frozen=True)
class MediaProvenance:
    """Media identity and offsets a transcript finalizer can store."""

    media_object_id: int
    media_start_offset_ms: int
    media_end_offset_ms: int


class ObjectStorage(Protocol):
    """Shared object storage. The recorder chooses the key."""

    def object_uri(self, key: str) -> str:
        """Return the durable URI for a server-owned key."""

    async def start_upload(self, key: str, content_type: str) -> ObjectUpload:
        """Open a multipart upload."""

    async def upload_part(
        self, upload: ObjectUpload, part_number: int, body: bytes
    ) -> UploadedPart:
        """Upload one part. The same part number may be sent again."""

    async def complete_upload(self, upload: ObjectUpload, parts: Sequence[UploadedPart]) -> None:
        """Make the uploaded parts readable as one object."""

    async def abort_upload(self, upload: ObjectUpload) -> None:
        """Drop an upload that will not be completed."""


class MediaCatalog(Protocol):
    """Persistence for one media-object row. Calls are short and synchronous."""

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
        """Insert an open row and return its id."""

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
        """Mark an open row complete."""

    def fail_media(self, tenant_id: int, session_id: int, media_object_id: int) -> None:
        """Mark an open row failed."""


class _Stop:
    """Tells the writer to upload the tail and finish."""


@dataclass(frozen=True)
class _Queued:
    pcm: bytes
    queued_at: float


def _table(model: type[Base]) -> Table:
    relation = model.__table__
    if not isinstance(relation, Table):
        raise TypeError(model.__name__)
    return relation


class DatabaseMediaCatalog:
    """Write ``media_objects`` through a short transaction per state change."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
        media = _table(MediaObject)
        with self._engine.begin() as connection:
            media_id = connection.execute(
                insert(media)
                .values(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    storage_uri=storage_uri,
                    content_type=content_type,
                    codec=codec,
                    sample_rate_hz=sample_rate_hz,
                    channel_count=channel_count,
                )
                .returning(media.c.media_object_id)
            ).scalar_one()
        return int(media_id)

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
        media = _table(MediaObject)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(media)
                .where(
                    media.c.tenant_id == tenant_id,
                    media.c.session_id == session_id,
                    media.c.media_object_id == media_object_id,
                    media.c.write_status == "open",
                )
                .values(
                    write_status="complete",
                    media_started_at=media_started_at,
                    media_ended_at=media_ended_at,
                    byte_length=byte_length,
                    checksum=checksum,
                )
            )
        if result.rowcount != 1:
            raise RecordingError("media object was not open")

    def fail_media(self, tenant_id: int, session_id: int, media_object_id: int) -> None:
        media = _table(MediaObject)
        with self._engine.begin() as connection:
            connection.execute(
                update(media)
                .where(
                    media.c.tenant_id == tenant_id,
                    media.c.session_id == session_id,
                    media.c.media_object_id == media_object_id,
                    media.c.write_status == "open",
                )
                .values(write_status="failed")
            )


class S3ObjectStorage:
    """S3 multipart adapter. The boto client stays inside this class."""

    def __init__(self, client: Any, *, bucket: str, prefix: str) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def object_uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{self._object_key(key)}"

    async def start_upload(self, key: str, content_type: str) -> ObjectUpload:
        response = await asyncio.to_thread(
            partial(
                self._client.create_multipart_upload,
                Bucket=self._bucket,
                Key=self._object_key(key),
                ContentType=content_type,
            )
        )
        return ObjectUpload(key=key, upload_id=str(response["UploadId"]))

    async def upload_part(
        self, upload: ObjectUpload, part_number: int, body: bytes
    ) -> UploadedPart:
        response = await asyncio.to_thread(
            partial(
                self._client.upload_part,
                Bucket=self._bucket,
                Key=self._object_key(upload.key),
                UploadId=upload.upload_id,
                PartNumber=part_number,
                Body=body,
            )
        )
        return UploadedPart(part_number=part_number, etag=str(response["ETag"]))

    async def complete_upload(self, upload: ObjectUpload, parts: Sequence[UploadedPart]) -> None:
        await asyncio.to_thread(
            partial(
                self._client.complete_multipart_upload,
                Bucket=self._bucket,
                Key=self._object_key(upload.key),
                UploadId=upload.upload_id,
                MultipartUpload={
                    "Parts": [{"PartNumber": part.part_number, "ETag": part.etag} for part in parts]
                },
            )
        )

    async def abort_upload(self, upload: ObjectUpload) -> None:
        await asyncio.to_thread(
            partial(
                self._client.abort_multipart_upload,
                Bucket=self._bucket,
                Key=self._object_key(upload.key),
                UploadId=upload.upload_id,
            )
        )

    def _object_key(self, key: str) -> str:
        if self._prefix:
            return f"{self._prefix}/{key}"
        return key


def s3_object_storage(
    settings: HotServerSettings, recording: RecordingConfig | None = None
) -> S3ObjectStorage:
    """Build an S3 adapter from process settings.

    A part size below 5 MiB is rejected. S3 allows a smaller body only for
    the last part of an upload, which the recorder sends on finish.
    """
    config = recording if recording is not None else RecordingConfig()
    if config.part_bytes < S3_MIN_PART_BYTES:
        raise ConfigurationError(
            f"recording part size must be at least {S3_MIN_PART_BYTES} bytes for S3"
        )
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    parsed = urlparse(settings.media_storage_uri)
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=Config(s3={"addressing_style": "path" if settings.s3_use_path_style else "virtual"}),
    )
    return S3ObjectStorage(client, bucket=parsed.netloc, prefix=parsed.path)


def _require_source_id(source_id: str) -> None:
    if not source_id or source_id != source_id.strip() or "/" in source_id or "\\" in source_id:
        raise RecordingError("source_id must be a single path segment")


class DirectSourceRecording:
    """Record one direct-source attachment to a single durable object.

    ``accept`` enqueues PCM and returns. A writer task uploads parts. The
    queue lives in this process and is capped by byte count and by the age
    of the oldest queued chunk.
    """

    def __init__(
        self,
        *,
        tenant_id: int,
        session_id: int,
        source_id: str,
        session_started_at: datetime,
        storage: ObjectStorage,
        catalog: MediaCatalog,
        config: RecordingConfig | None = None,
        audio: DirectSourcePipelineConfig = DIRECT_SOURCE_PIPELINE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        _require_source_id(source_id)
        if session_started_at.tzinfo is None:
            raise RecordingError("session_started_at must be timezone-aware")
        self._tenant_id = tenant_id
        self._session_id = session_id
        self._source_id = source_id
        self._session_started_at = session_started_at
        self._storage = storage
        self._catalog = catalog
        self._config = config if config is not None else RecordingConfig()
        self._audio = audio
        self._clock = clock if clock is not None else time.monotonic
        self._bytes_per_second = audio.sample_rate * audio.sample_width * audio.channels
        self._gate = asyncio.Condition()
        self._chunks: deque[_Queued | _Stop] = deque()
        self._queued_bytes = 0
        self._writer: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._finish_started = False
        self._media_id: int | None = None
        self._upload: ObjectUpload | None = None
        self._origin_ms: int | None = None
        self._accepted_bytes = 0
        self._written = 0
        self._hasher = hashlib.sha256()

    async def accept(self, pcm: bytes, *, timestamp_ms: int) -> None:
        """Queue one PCM chunk. The first chunk opens the object."""
        if timestamp_ms < 0:
            raise RecordingError("timestamp_ms must be non-negative")
        if not pcm:
            raise RecordingError("PCM is empty")
        if len(pcm) % self._audio.sample_width != 0:
            raise RecordingError("PCM length must cover whole samples")
        async with self._gate:
            self._raise_if_stopped()
            if self._media_id is None:
                await self._open(timestamp_ms)
                self._raise_if_stopped()
            await self._enforce_bounds(pcm)
            self._accepted_bytes += len(pcm)
            self._chunks.append(_Queued(pcm, self._clock()))
            self._queued_bytes += len(pcm)
            self._gate.notify_all()
            media_id = self._media_id
        logger.debug(
            "audio frame queued",
            tenant_id=self._tenant_id,
            session_id=self._session_id,
            source_id=self._source_id,
            media_id=media_id,
            timestamp_ms=timestamp_ms,
            byte_count=len(pcm),
        )

    async def finish(self) -> None:
        """Upload the tail and mark the row complete.

        A recording that already failed stays failed and raises.
        """
        async with self._gate:
            writer = self._writer
            if not self._finish_started:
                self._finish_started = True
                if writer is not None:
                    self._chunks.append(_Stop())
                    self._gate.notify_all()
        if writer is not None:
            await writer
        if self._failure is not None:
            raise RecordingError(str(self._failure)) from self._failure

    def provenance(self, start_ms: int, end_ms: int) -> MediaProvenance:
        """Return offsets from the first recorded sample.

        The row may still be open. ``start_ms`` and ``end_ms`` are session
        timeline milliseconds, the same clock as ``accept``.
        """
        if end_ms < start_ms:
            raise RecordingError("end_ms must not precede start_ms")
        if self._media_id is None or self._origin_ms is None:
            raise RecordingError("recording has no media object")
        start_offset = start_ms - self._origin_ms
        end_offset = end_ms - self._origin_ms
        duration_ms = self._duration_ms(self._accepted_bytes)
        if start_offset < 0 or end_offset > duration_ms:
            raise RecordingError("media interval is outside the recording")
        return MediaProvenance(self._media_id, start_offset, end_offset)

    async def _open(self, timestamp_ms: int) -> None:
        key = f"{self._tenant_id}/{self._session_id}/{self._source_id}/{uuid4()}.pcm"
        try:
            media_id = await asyncio.to_thread(
                self._catalog.open_media,
                self._tenant_id,
                self._session_id,
                self._storage.object_uri(key),
                CONTENT_TYPE,
                CODEC,
                self._audio.sample_rate,
                self._audio.channels,
            )
        except Exception as error:
            await self._fail(error)
            raise RecordingError(str(error)) from error
        self._media_id = media_id
        self._origin_ms = timestamp_ms
        try:
            self._upload = await self._storage.start_upload(key, CONTENT_TYPE)
        except Exception as error:
            await self._fail(error)
            raise RecordingError(str(error)) from error
        self._writer = asyncio.create_task(self._run(), name=f"recording-{self._source_id}")
        logger.info(
            "recording started",
            tenant_id=self._tenant_id,
            session_id=self._session_id,
            source_id=self._source_id,
            media_id=media_id,
        )

    async def _enforce_bounds(self, pcm: bytes) -> None:
        if self._chunks:
            oldest = self._chunks[0]
            if isinstance(oldest, _Queued):
                age_ms = (self._clock() - oldest.queued_at) * 1000
                if age_ms > self._config.max_queued_age_ms:
                    await self._reject("recording queue is stale")
        if self._queued_bytes + len(pcm) > self._config.max_queued_bytes:
            await self._reject("recording queue is full")

    async def _reject(self, message: str) -> None:
        try:
            raise RecordingError(message)
        except RecordingError as error:
            await self._fail(error)
            raise

    async def _fail(self, error: BaseException) -> None:
        if self._failure is not None:
            return
        self._failure = error
        logger.exception(
            "recording failed",
            tenant_id=self._tenant_id,
            session_id=self._session_id,
            source_id=self._source_id,
            media_id=self._media_id,
            error=str(error),
        )
        upload = self._upload
        media_id = self._media_id
        if upload is not None:
            try:
                await self._storage.abort_upload(upload)
            except Exception as abort_error:
                logger.warning(
                    "recording abort failed",
                    tenant_id=self._tenant_id,
                    session_id=self._session_id,
                    source_id=self._source_id,
                    media_id=media_id,
                    error=str(abort_error),
                )
        if media_id is not None:
            try:
                await asyncio.to_thread(
                    self._catalog.fail_media,
                    self._tenant_id,
                    self._session_id,
                    media_id,
                )
            except Exception:
                logger.exception(
                    "recording failure was not persisted",
                    tenant_id=self._tenant_id,
                    session_id=self._session_id,
                    source_id=self._source_id,
                    media_id=media_id,
                )

    def _raise_if_stopped(self) -> None:
        if self._failure is not None:
            raise RecordingError(str(self._failure)) from self._failure
        if self._finish_started:
            raise RecordingError("recording is closed")

    async def _take(self) -> _Queued | _Stop:
        async with self._gate:
            while not self._chunks:
                await self._gate.wait()
            item = self._chunks.popleft()
            if isinstance(item, _Queued):
                self._queued_bytes -= len(item.pcm)
            return item

    async def _run(self) -> None:
        part = bytearray()
        part_number = 1
        uploaded: list[UploadedPart] = []
        try:
            while True:
                item = await self._take()
                if isinstance(item, _Stop):
                    break
                if self._failure is not None:
                    continue
                part.extend(item.pcm)
                self._hasher.update(item.pcm)
                self._written += len(item.pcm)
                while len(part) >= self._config.part_bytes:
                    body = bytes(part[: self._config.part_bytes])
                    del part[: self._config.part_bytes]
                    uploaded.append(await self._put_part(part_number, body))
                    part_number += 1
                    if self._failure is not None:
                        return
            if self._failure is not None:
                return
            if part:
                uploaded.append(await self._put_part(part_number, bytes(part)))
                if self._failure is not None:
                    return
            if not uploaded or self._upload is None:
                return
            await self._storage.complete_upload(self._upload, tuple(uploaded))
            await self._mark_complete()
        except Exception as error:
            async with self._gate:
                await self._fail(error)

    async def _put_part(self, part_number: int, body: bytes) -> UploadedPart:
        upload = self._upload
        if upload is None:
            raise RecordingError("recording upload is not open")
        try:
            uploaded = await self._storage.upload_part(upload, part_number, body)
        except Exception as error:
            logger.warning(
                "recording part upload retry",
                tenant_id=self._tenant_id,
                session_id=self._session_id,
                source_id=self._source_id,
                media_id=self._media_id,
                error=str(error),
            )
            uploaded = await self._storage.upload_part(upload, part_number, body)
        logger.debug(
            "recording part uploaded",
            tenant_id=self._tenant_id,
            session_id=self._session_id,
            source_id=self._source_id,
            media_id=self._media_id,
            byte_count=len(body),
        )
        return uploaded

    async def _mark_complete(self) -> None:
        async with self._gate:
            if self._failure is not None or self._media_id is None or self._origin_ms is None:
                return
            if self._written != self._accepted_bytes:
                await self._fail(RecordingError("recorded bytes were not all uploaded"))
                return
            media_id = self._media_id
            started = self._session_started_at + timedelta(milliseconds=self._origin_ms)
            ended = started + timedelta(milliseconds=self._duration_ms(self._accepted_bytes))
            checksum = "sha256:" + self._hasher.hexdigest()
            byte_length = self._accepted_bytes
        await asyncio.to_thread(
            self._catalog.complete_media,
            self._tenant_id,
            self._session_id,
            media_id,
            started,
            ended,
            byte_length,
            checksum,
        )
        logger.info(
            "recording completed",
            tenant_id=self._tenant_id,
            session_id=self._session_id,
            source_id=self._source_id,
            media_id=media_id,
            byte_count=byte_length,
        )

    def _duration_ms(self, byte_count: int) -> int:
        return byte_count * 1000 // self._bytes_per_second
