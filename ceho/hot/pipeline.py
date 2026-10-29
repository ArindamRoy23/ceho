"""Pipecat construction and per-source lifecycle for one direct audio source.

The run accepts ordered 16 kHz 16-bit mono PCM and emits final transcript
candidates. Interim text stays on the run. This module does not write the
database, publish events, or produce talkback audio.

The processors in front of the candidate sink are injected. The live path
uses Deepgram speech-to-text and nothing else: no voice activity detector,
diarization, language model, or speech synthesizer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ceho.hot.config import (
    DIRECT_SOURCE_PIPELINE,
    ConfigurationError,
    DirectSourcePipelineConfig,
)

logger = logging.getLogger(__name__)


class PipelineRunError(RuntimeError):
    """Raised when a pipeline run is used outside its lifecycle."""


class RunState(Enum):
    """Lifecycle of one direct-source pipeline run."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class DirectSourceContext:
    """Session and membership a direct source is allowed to attribute.

    ``session_user_id`` is the expected membership for this source. It is not
    a diarized speaker and it is not proof that nobody else used the microphone.
    """

    tenant_id: int
    session_id: int
    source_id: str
    session_user_id: int | None


@dataclass(frozen=True)
class TranscriptCandidate:
    """One final, non-empty transcript ready for a later persistence step."""

    context: DirectSourceContext
    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not self.text or self.text != self.text.strip():
            raise ValueError("transcript text must be non-empty and stripped")
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not precede start_ms")


@dataclass(frozen=True)
class PipelineFailure:
    """A provider or pipeline error observed while a run was active."""

    context: DirectSourceContext
    message: str
    fatal: bool


@dataclass(frozen=True)
class RunStopped:
    """The source closed and the pipeline run finished.

    Closing a run does not end the durable session. ``ends_session`` is
    therefore always false.
    """

    context: DirectSourceContext
    state: RunState

    @property
    def ends_session(self) -> bool:
        return False


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _field(obj: object, name: str) -> object:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _speech_bounds_seconds(result: object) -> tuple[float, float] | None:
    """Read a provider result's speech interval, in seconds from audio start."""
    if result is None:
        return None
    channel = _field(result, "channel")
    alternatives = _field(channel, "alternatives") if channel is not None else None
    words = None
    if isinstance(alternatives, list) and alternatives:
        words = _field(alternatives[0], "words")
    if isinstance(words, list) and words:
        start = _as_float(_field(words[0], "start"))
        end = _as_float(_field(words[-1], "end"))
        if start is not None and end is not None:
            return start, end
    start = _as_float(_field(result, "start"))
    duration = _as_float(_field(result, "duration"))
    if start is not None and duration is not None:
        return start, start + duration
    return None


class _FinalCandidateSink(FrameProcessor):
    """Turn final transcription frames into CEHO candidates.

    Interim frames update in-memory partial text and are not candidates.
    """

    def __init__(self, run: DirectSourceRun) -> None:
        super().__init__()
        self._run = run

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame):
            self._run._note_partial(frame.text)
        elif isinstance(frame, TranscriptionFrame):
            self._run._note_final(frame)
        await self.push_frame(frame, direction)


class DirectSourceRun:
    """Start, feed, and stop one Pipecat task for a single direct source."""

    def __init__(
        self,
        context: DirectSourceContext,
        processors: Sequence[FrameProcessor],
        *,
        config: DirectSourcePipelineConfig = DIRECT_SOURCE_PIPELINE,
    ) -> None:
        self._context = context
        self._config = config
        self._processors = tuple(processors)
        self._state = RunState.IDLE
        self._accepting_candidates = True
        self._close_started = False
        self._cancel_sent = False
        self._failure_count = 0
        self._origin_ms: int | None = None
        self._partial_text = ""
        self._seen: set[tuple[int, int, str]] = set()
        self._candidates: list[TranscriptCandidate] = []
        self._failures: list[PipelineFailure] = []
        self._task: PipelineTask | None = None
        self._run_task: asyncio.Task[None] | None = None

    @property
    def context(self) -> DirectSourceContext:
        return self._context

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def partial_text(self) -> str:
        """Latest interim hypothesis. This is not a final candidate."""
        return self._partial_text

    @property
    def candidates(self) -> tuple[TranscriptCandidate, ...]:
        return tuple(self._candidates)

    @property
    def failures(self) -> tuple[PipelineFailure, ...]:
        return tuple(self._failures)

    async def start(self, *, handle_signals: bool = False) -> None:
        """Construct the task and start supervising it.

        Signal handling stays off unless a standalone process owns the
        terminal. The hot server owns its own shutdown.
        """
        if self._state is not RunState.IDLE or self._close_started:
            raise PipelineRunError("pipeline run cannot start")
        pipeline = Pipeline([*self._processors, _FinalCandidateSink(self)])
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=self._config.sample_rate,
                audio_out_sample_rate=self._config.sample_rate,
                enable_heartbeats=False,
                enable_metrics=False,
                enable_usage_metrics=False,
            ),
            cancel_on_idle_timeout=False,
            check_dangling_tasks=False,
            conversation_id=self._context.source_id,
            enable_rtvi=False,
            enable_tracing=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
        )

        @task.event_handler("on_pipeline_error")
        async def on_pipeline_error(_task: object, frame: ErrorFrame) -> None:
            await self._on_provider_error(frame)

        self._task = task
        runner = PipelineRunner(handle_sigint=handle_signals, handle_sigterm=False)
        self._run_task = asyncio.create_task(
            runner.run(task),
            name=f"direct-source-{self._context.source_id}",
        )
        self._state = RunState.RUNNING

    async def push_audio(self, pcm: bytes, *, timestamp_ms: int) -> None:
        """Queue one contiguous chunk of 16 kHz 16-bit mono PCM.

        ``timestamp_ms`` is the session time of the first sample. The first
        chunk's timestamp is the origin for provider-relative speech intervals.
        """
        if self._state is not RunState.RUNNING or self._close_started or self._task is None:
            raise PipelineRunError("pipeline run is not accepting audio")
        if not pcm:
            return
        if len(pcm) % self._config.sample_width != 0:
            raise PipelineRunError("PCM length must cover whole samples")
        if self._origin_ms is None:
            self._origin_ms = timestamp_ms
        await self._task.queue_frame(
            InputAudioRawFrame(
                audio=pcm,
                sample_rate=self._config.sample_rate,
                num_channels=self._config.channels,
            )
        )

    async def close(self) -> RunStopped:
        """Stop the run after queued audio has been processed.

        The durable session stays active. A run that already failed stays
        failed and is not reported as a clean stop.
        """
        if not self._close_started:
            self._close_started = True
            if self._state is RunState.RUNNING and self._task is not None:
                await self._task.stop_when_done()
            await self._finish_run_task()
            if self._state is RunState.RUNNING:
                self._state = RunState.STOPPED
            self._accepting_candidates = False
        else:
            await self._finish_run_task()
        return RunStopped(self._context, self._state)

    def _note_partial(self, text: str) -> None:
        if self._accepting_candidates:
            self._partial_text = text

    def _note_final(self, frame: TranscriptionFrame) -> None:
        if not self._accepting_candidates:
            return
        text = frame.text.strip()
        if not text:
            return
        start_ms, end_ms = self._interval_ms(frame.result)
        key = (start_ms, end_ms, text)
        if key in self._seen:
            return
        self._seen.add(key)
        self._candidates.append(TranscriptCandidate(self._context, text, start_ms, end_ms))

    def _interval_ms(self, result: object) -> tuple[int, int]:
        origin = 0 if self._origin_ms is None else self._origin_ms
        bounds = _speech_bounds_seconds(result)
        if bounds is None:
            return origin, origin
        start_ms = origin + int(bounds[0] * 1000)
        end_ms = origin + int(bounds[1] * 1000)
        if end_ms < start_ms:
            end_ms = start_ms
        return start_ms, end_ms

    async def _on_provider_error(self, frame: ErrorFrame) -> None:
        if self._state is RunState.FAILED:
            return
        message = frame.error.strip() if isinstance(frame.error, str) else ""
        if not message:
            message = "STT provider error"
        self._failure_count += 1
        fatal = bool(frame.fatal) or self._failure_count >= self._config.max_provider_failures
        if fatal:
            self._accepting_candidates = False
            self._state = RunState.FAILED
        self._failures.append(PipelineFailure(self._context, message, fatal))
        logger.warning("STT provider error for source %s: %s", self._context.source_id, message)
        if fatal and self._task is not None and not self._cancel_sent:
            self._cancel_sent = True
            await self._task.cancel(reason=message)

    async def _finish_run_task(self) -> None:
        task = self._run_task
        if task is None:
            return
        try:
            await task
        except Exception as error:
            self._record_fatal(str(error) or "pipeline run failed")

    def _record_fatal(self, message: str) -> None:
        if self._state is RunState.FAILED:
            return
        self._accepting_candidates = False
        self._state = RunState.FAILED
        self._failures.append(PipelineFailure(self._context, message, True))
        logger.warning("Pipeline run failed for source %s: %s", self._context.source_id, message)


def deepgram_stt(
    api_key: str,
    *,
    config: DirectSourcePipelineConfig = DIRECT_SOURCE_PIPELINE,
) -> FrameProcessor:
    """Build the Deepgram speech-to-text processor for one direct source.

    The processor emits interim and final transcriptions. It does not speak
    and it does not call a language model. Diarization is off.
    """
    key = api_key.strip()
    if not key:
        raise ConfigurationError("DEEPGRAM_API_KEY must be configured")
    from pipecat.services.deepgram.stt import DeepgramSTTService

    class _DeepgramSTT(DeepgramSTTService):
        """Send the language tag's value. Python 3.10 stringifies the enum name."""

        def _build_connect_kwargs(self) -> dict[str, Any]:
            kwargs = super()._build_connect_kwargs()
            language = getattr(self._settings, "language", None)
            tag = getattr(language, "value", language)
            if isinstance(tag, str):
                kwargs["language"] = tag
            return kwargs

    return _DeepgramSTT(
        api_key=key,
        encoding="linear16",
        channels=config.channels,
        sample_rate=config.sample_rate,
        settings=DeepgramSTTService.Settings(
            language="en",
            interim_results=True,
            punctuate=True,
            diarize=False,
            vad_events=False,
            endpointing=300,
        ),
    )
