"""Tests for the direct-source Pipecat STT run."""

import ast
import asyncio
import unittest
from pathlib import Path
from typing import Any, cast

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ceho.hot.config import DIRECT_SOURCE_PIPELINE, ConfigurationError
from ceho.hot.pipeline import (
    DirectSourceContext,
    DirectSourceRun,
    RunState,
    RunStopped,
    deepgram_stt,
)


def _context() -> DirectSourceContext:
    return DirectSourceContext(
        tenant_id=1,
        session_id=10,
        source_id="mic",
        session_speaker_id=7,
    )


class _Word:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class _Alternative:
    def __init__(self, words: list[_Word]) -> None:
        self.words = words


class _Channel:
    def __init__(self, words: list[_Word]) -> None:
        self.alternatives = [_Alternative(words)]


class _Result:
    def __init__(
        self,
        start: float,
        duration: float,
        words: list[_Word] | None = None,
    ) -> None:
        self.start = start
        self.duration = duration
        self.channel = _Channel(words) if words else None


def _transcription(
    text: str,
    *,
    start: float,
    duration: float,
    words: list[tuple[float, float]] | None = None,
) -> TranscriptionFrame:
    word_objs = None if words is None else [_Word(start_s, end_s) for start_s, end_s in words]
    return TranscriptionFrame(
        text,
        "user",
        "2026-01-01T00:00:00Z",
        result=_Result(start, duration, word_objs),
    )


class ScriptedSTT(FrameProcessor):
    """Emit a fixed frame list the first time audio arrives."""

    def __init__(self, frames: list[Frame], *, errors: int = 0) -> None:
        super().__init__()
        self._frames = frames
        self._errors = errors
        self._done = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and not self._done:
            self._done = True
            for _ in range(self._errors):
                await self.push_error("provider unavailable")
            for emitted in self._frames:
                await self.push_frame(emitted, direction)
        await self.push_frame(frame, direction)


class Marker(FrameProcessor):
    """Stand-in for a stage that can be inserted without changing candidates."""

    def __init__(self) -> None:
        super().__init__()
        self.saw_audio = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.saw_audio = True
        await self.push_frame(frame, direction)


class PipelineContractTest(unittest.TestCase):
    """Verify the candidate contract without running a pipeline task."""

    def test_pipeline_module_does_not_import_persistence(self) -> None:
        source = Path("ceho/hot/pipeline.py").read_text()
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        forbidden = (
            "sqlalchemy",
            "ceho.models",
            "ceho.hot.session",
            "ceho.hot.outbox",
            "ceho.hot.media",
        )
        for name in imported:
            for prefix in forbidden:
                self.assertFalse(name == prefix or name.startswith(prefix + "."), name)
            self.assertNotIn("llm", name)
            self.assertNotIn("tts", name)

    def test_deepgram_stt_requires_an_api_key(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DEEPGRAM_API_KEY"):
            deepgram_stt("  ")

    def test_deepgram_stt_builds_without_connecting(self) -> None:
        processor = deepgram_stt("test-key")

        self.assertIsInstance(processor, FrameProcessor)
        kwargs = cast(Any, processor)._build_connect_kwargs()
        self.assertEqual(kwargs["language"], "en")


class DirectSourceRunTest(unittest.IsolatedAsyncioTestCase):
    """Verify one direct source can emit, reject, and stop."""

    @classmethod
    def setUpClass(cls) -> None:
        logger.remove()

    async def _collect(
        self,
        processors: list[FrameProcessor],
        *,
        pcm: bytes | None = b"\x00\x00",
        timestamp_ms: int = 1000,
    ) -> tuple[DirectSourceRun, RunStopped]:
        context = _context()
        run = DirectSourceRun(context, processors)
        await run.start()
        try:
            if pcm is not None:
                await run.push_audio(pcm, timestamp_ms=timestamp_ms)
        finally:
            stopped = await asyncio.wait_for(run.close(), timeout=5)
        return run, stopped

    async def test_final_candidate_uses_provider_interval_and_membership(self) -> None:
        run, stopped = await self._collect(
            [ScriptedSTT([_transcription("hello", start=0.5, duration=0.4)])]
        )

        self.assertEqual(len(run.candidates), 1)
        candidate = run.candidates[0]
        self.assertEqual(candidate.text, "hello")
        self.assertEqual(candidate.start_ms, 1500)
        self.assertEqual(candidate.end_ms, 1900)
        self.assertEqual(candidate.context.session_speaker_id, 7)
        self.assertIs(candidate.context, run.context)
        self.assertFalse(stopped.ends_session)
        self.assertIs(stopped.state, RunState.STOPPED)

    async def test_word_bounds_override_the_result_interval(self) -> None:
        run, _stopped = await self._collect(
            [
                ScriptedSTT(
                    [
                        _transcription(
                            "hello",
                            start=0.0,
                            duration=1.0,
                            words=[(0.2, 0.6)],
                        )
                    ]
                )
            ]
        )

        self.assertEqual(run.candidates[0].start_ms, 1200)
        self.assertEqual(run.candidates[0].end_ms, 1600)

    async def test_empty_text_is_rejected_and_partials_stay_inside(self) -> None:
        run, _stopped = await self._collect(
            [
                ScriptedSTT(
                    [
                        InterimTranscriptionFrame("hel", "user", "2026-01-01T00:00:00Z"),
                        _transcription("   \n", start=0.0, duration=0.2),
                        _transcription("hello", start=0.5, duration=0.4),
                    ]
                )
            ]
        )

        self.assertEqual([candidate.text for candidate in run.candidates], ["hello"])
        self.assertEqual(run.partial_text, "hel")

    async def test_duplicate_finals_collapse_and_distinct_intervals_remain(self) -> None:
        run, _stopped = await self._collect(
            [
                ScriptedSTT(
                    [
                        _transcription("hello", start=0.0, duration=0.5),
                        _transcription("hello", start=0.0, duration=0.5),
                        _transcription("hello", start=1.0, duration=0.5),
                    ]
                )
            ]
        )

        self.assertEqual(
            [
                (candidate.start_ms, candidate.end_ms, candidate.text)
                for candidate in run.candidates
            ],
            [(1000, 1500, "hello"), (2000, 2500, "hello")],
        )

    async def test_processor_inserted_ahead_of_stt_keeps_the_candidate(self) -> None:
        marker = Marker()
        run, _stopped = await self._collect(
            [marker, ScriptedSTT([_transcription("hello", start=0.0, duration=0.2)])]
        )

        self.assertTrue(marker.saw_audio)
        self.assertEqual(run.candidates[0].text, "hello")

    async def test_provider_errors_are_bounded_and_drop_later_candidates(self) -> None:
        run, stopped = await self._collect(
            [
                ScriptedSTT(
                    [_transcription("nope", start=0.0, duration=0.2)],
                    errors=DIRECT_SOURCE_PIPELINE.max_provider_failures,
                )
            ]
        )

        self.assertEqual(run.candidates, ())
        self.assertEqual(len(run.failures), DIRECT_SOURCE_PIPELINE.max_provider_failures)
        self.assertFalse(run.failures[0].fatal)
        self.assertTrue(run.failures[-1].fatal)
        self.assertIs(stopped.state, RunState.FAILED)
        self.assertFalse(stopped.ends_session)

    async def test_one_provider_error_still_emits_a_final_candidate(self) -> None:
        run, stopped = await self._collect(
            [
                ScriptedSTT(
                    [_transcription("hello", start=0.0, duration=0.2)],
                    errors=1,
                )
            ]
        )

        self.assertEqual([candidate.text for candidate in run.candidates], ["hello"])
        self.assertEqual(len(run.failures), 1)
        self.assertFalse(run.failures[0].fatal)
        self.assertIs(stopped.state, RunState.STOPPED)

    async def test_source_closure_stops_the_run_without_ending_the_session(self) -> None:
        context = _context()
        run = DirectSourceRun(context, [])
        await run.start()
        stopped = await asyncio.wait_for(run.close(), timeout=5)

        self.assertFalse(stopped.ends_session)
        self.assertIs(stopped.state, RunState.STOPPED)
        self.assertEqual(run.candidates, ())
        with self.assertRaisesRegex(Exception, "not accepting audio"):
            await run.push_audio(b"\x00\x00", timestamp_ms=0)
