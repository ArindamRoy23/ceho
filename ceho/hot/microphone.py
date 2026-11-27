"""Attach one pipeline run and one recorder to a connected direct microphone.

The transport port delivers ordered ``pcm_s16le``. This module resamples that
PCM to the pipeline format, pushes it, and commits each final candidate.
It does not import the WebRTC adapter and it does not end the durable session.
"""

from __future__ import annotations

import asyncio
import audioop
from collections.abc import Sequence
from typing import Protocol

from pipecat.processors.frame_processor import FrameProcessor

from ceho.hot.config import DIRECT_SOURCE_PIPELINE, DirectSourcePipelineConfig
from ceho.hot.media import MediaProvenance, RecordingError
from ceho.hot.pipeline import (
    DirectSourceContext,
    DirectSourceRecorder,
    DirectSourceRun,
    PipelineRunError,
    RunState,
    RunStopped,
    TranscriptCandidate,
    deepgram_stt,
)
from ceho.hot.transport import (
    CloseReason,
    DirectSourceTransport,
    OrderedFrame,
    PeerClosed,
    QueuePressure,
    SourceAdded,
    SourceRemoved,
)
from ceho.logging import get_logger

logger = get_logger(__name__)


class MicrophoneRecorder(DirectSourceRecorder, Protocol):
    """Recorder that can also name the media interval for one final."""

    def provenance(self, start_ms: int, end_ms: int) -> MediaProvenance:
        """Return the media id and offsets for a session-timeline interval."""


class TranscriptCommit(Protocol):
    """Persist one final and return the session speaker id to reuse."""

    def __call__(
        self,
        candidate: TranscriptCandidate,
        *,
        session_speaker_id: int | None,
        provenance: MediaProvenance | None,
    ) -> int:
        """Commit one final transcript."""


class _Resampler:
    """Convert ordered ``pcm_s16le`` to 16 kHz 16-bit mono.

    The rate converter keeps its filter state across frames of the same rate.
    A timestamp gap is not filled with silence here. The caller passes the
    source timestamp through unchanged.
    """

    def __init__(self, config: DirectSourcePipelineConfig) -> None:
        self._config = config
        self._state: tuple[int, tuple[tuple[int, int], ...]] | None = None
        self._rate: int | None = None

    def convert(self, pcm: bytes, *, sample_rate: int, channel_count: int) -> bytes:
        width = self._config.sample_width
        if sample_rate < 1 or channel_count < 1:
            raise ValueError("PCM format is incomplete")
        if channel_count == 2:
            if len(pcm) % (width * 2) != 0:
                raise ValueError("PCM length must cover whole frames")
            pcm = audioop.tomono(pcm, width, 0.5, 0.5)
        elif channel_count == 1:
            if len(pcm) % width != 0:
                raise ValueError("PCM length must cover whole samples")
        else:
            raise ValueError("PCM channel count must be 1 or 2")
        if sample_rate == self._config.sample_rate:
            return pcm
        if self._rate != sample_rate:
            self._state = None
            self._rate = sample_rate
        converted, self._state = audioop.ratecv(
            pcm,
            width,
            self._config.channels,
            sample_rate,
            self._config.sample_rate,
            self._state,
        )
        return converted


def recording_source_id(source_id: str) -> str:
    """Return one path segment for a transport source id.

    The transport id is ``{peer_id}/microphone``. The recorder keeps that id
    as a single segment so the object key cannot gain an extra path piece.
    """
    safe = source_id.strip().replace("/", "-").replace("\\", "-")
    if not safe:
        raise RecordingError("source_id must be a single path segment")
    return safe


def direct_source_processors(api_key: str | None) -> tuple[FrameProcessor, ...]:
    """Build the live speech-to-text processor, or none when the key is absent."""
    if api_key is None or not api_key.strip():
        return ()
    return (deepgram_stt(api_key),)


async def attach_microphone(
    transport: DirectSourceTransport,
    *,
    processors: Sequence[FrameProcessor],
    recorder: MicrophoneRecorder,
    commit: TranscriptCommit,
    config: DirectSourcePipelineConfig = DIRECT_SOURCE_PIPELINE,
) -> RunStopped | None:
    """Read one microphone until the source or the peer closes.

    Closing the run finishes the recorder. The returned stop never ends the
    durable session. A recording failure cancels the run and closes the peer.
    A provider failure stays on the run's own failure budget.
    """
    resampler = _Resampler(config)
    run: DirectSourceRun | None = None
    stopped: RunStopped | None = None
    speaker_id: int | None = None
    committed = 0
    run_closed = False
    peer_closed = False

    async def commit_ready() -> None:
        nonlocal committed, speaker_id
        if run is None:
            return
        fresh = run.candidates[committed:]
        committed += len(fresh)
        for candidate in fresh:
            try:
                provenance: MediaProvenance | None = recorder.provenance(
                    candidate.start_ms,
                    candidate.end_ms,
                )
            except Exception as error:
                logger.error(
                    "transcript rejected",
                    tenant_id=candidate.context.tenant_id,
                    session_id=candidate.context.session_id,
                    source_id=candidate.context.source_id,
                    error=str(error),
                )
                continue
            try:
                speaker_id = await asyncio.to_thread(
                    commit,
                    candidate,
                    session_speaker_id=speaker_id,
                    provenance=provenance,
                )
            except Exception:
                logger.exception(
                    "transcript commit failed",
                    tenant_id=candidate.context.tenant_id,
                    session_id=candidate.context.session_id,
                    source_id=candidate.context.source_id,
                )

    async def stop_run() -> None:
        nonlocal run_closed, stopped
        if run is None or run_closed:
            return
        run_closed = True
        stopped = await run.close()
        await commit_ready()
        logger.info(
            "microphone stopped",
            tenant_id=run.context.tenant_id,
            session_id=run.context.session_id,
            source_id=run.context.source_id,
            state=stopped.state.value,
            ends_session=stopped.ends_session,
        )

    try:
        async for event in transport.events():
            if isinstance(event, SourceAdded):
                if run is not None:
                    logger.error(
                        "microphone rejected",
                        tenant_id=event.tenant_id,
                        session_id=event.session_id,
                        source_id=event.source_id,
                        error="microphone is already attached",
                    )
                    continue
                if not processors:
                    logger.warning(
                        "stt not configured",
                        tenant_id=event.tenant_id,
                        session_id=event.session_id,
                        source_id=event.source_id,
                    )
                run = DirectSourceRun(
                    DirectSourceContext(
                        tenant_id=event.tenant_id,
                        session_id=event.session_id,
                        source_id=event.source_id,
                        session_speaker_id=None,
                    ),
                    processors,
                    config=config,
                    recorder=recorder,
                )
                try:
                    await run.start()
                except Exception:
                    logger.exception(
                        "microphone failed",
                        tenant_id=event.tenant_id,
                        session_id=event.session_id,
                        source_id=event.source_id,
                    )
                    run = None
                    if not peer_closed:
                        peer_closed = True
                        await transport.close(CloseReason.MEDIA_FAILED)
                    continue
                logger.info(
                    "microphone attached",
                    tenant_id=event.tenant_id,
                    session_id=event.session_id,
                    source_id=event.source_id,
                )
                continue
            if isinstance(event, QueuePressure):
                logger.warning(
                    "transport queue pressure",
                    tenant_id=event.tenant_id,
                    session_id=event.session_id,
                    source_id=event.source_id,
                    dropped_frames=event.dropped_frames,
                    queued_frames=event.queued_frames,
                    reason=event.reason,
                )
                continue
            if isinstance(event, OrderedFrame):
                if run is None or run.state is not RunState.RUNNING or run_closed:
                    logger.debug(
                        "audio frame dropped",
                        source_id=event.frame.source_id,
                        timestamp_ms=event.frame.timestamp_ms,
                        reason="run stopped",
                    )
                    continue
                frame = event.frame
                if frame.codec != "pcm_s16le":
                    logger.error(
                        "audio frame rejected",
                        source_id=frame.source_id,
                        timestamp_ms=frame.timestamp_ms,
                        error="PCM codec must be pcm_s16le",
                    )
                    continue
                try:
                    pcm = resampler.convert(
                        frame.payload,
                        sample_rate=frame.sample_rate,
                        channel_count=frame.channel_count,
                    )
                except Exception as error:
                    logger.error(
                        "audio frame rejected",
                        source_id=frame.source_id,
                        timestamp_ms=frame.timestamp_ms,
                        error=str(error) or "PCM could not be resampled",
                    )
                    continue
                if not pcm:
                    logger.debug(
                        "audio frame dropped",
                        source_id=frame.source_id,
                        timestamp_ms=frame.timestamp_ms,
                        reason="resampled audio is empty",
                    )
                    continue
                logger.debug(
                    "audio frame resampled",
                    source_id=frame.source_id,
                    timestamp_ms=frame.timestamp_ms,
                    sample_rate=frame.sample_rate,
                    channel_count=frame.channel_count,
                    byte_count=len(pcm),
                    discontinuity=frame.discontinuity,
                    gap_ms=frame.gap_ms,
                )
                try:
                    await run.push_audio(pcm, timestamp_ms=frame.timestamp_ms)
                except PipelineRunError as error:
                    message = str(error)
                    if message == "recording failed":
                        logger.error(
                            "microphone recording failed",
                            source_id=frame.source_id,
                            error=message,
                        )
                        await stop_run()
                        if not peer_closed:
                            peer_closed = True
                            await transport.close(CloseReason.MEDIA_FAILED)
                    else:
                        logger.error(
                            "audio frame rejected",
                            source_id=frame.source_id,
                            error=message,
                        )
                        await stop_run()
                    continue
                await commit_ready()
                continue
            if isinstance(event, (SourceRemoved, PeerClosed)):
                await stop_run()
    finally:
        await stop_run()
    return stopped
