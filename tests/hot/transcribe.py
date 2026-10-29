"""Send one prerecorded WAV through the direct-source Deepgram STT run.

This is a test harness, not part of the hot server. It prints candidates
and does not write the database.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import os
import sys
import wave
from pathlib import Path

from dotenv import load_dotenv

from ceho.hot.config import DIRECT_SOURCE_PIPELINE
from ceho.hot.pipeline import DirectSourceContext, DirectSourceRun, RunState, deepgram_stt

# How this file feeds a recording. The PCM format comes from the hot config.
FRAME_MS = 100
SILENCE_MS = 1000
CONNECT_TIMEOUT_SECS = 10.0
RESULT_TIMEOUT_SECS = 8.0
_AUDIO = DIRECT_SOURCE_PIPELINE


def pcm16_mono_16k(path: Path) -> bytes:
    """Read a WAV file and return 16 kHz 16-bit mono PCM."""
    if not path.is_file():
        raise ValueError(f"{path} is not a file")
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
    except wave.Error as error:
        raise ValueError(f"{path} is not a WAV file") from error
    if channels not in {1, 2}:
        raise ValueError(f"{path} must be mono or stereo")
    if sample_width not in {1, 2, 3, 4}:
        raise ValueError(f"{path} has an unsupported sample width")
    if sample_width != _AUDIO.sample_width:
        pcm = audioop.lin2lin(pcm, sample_width, _AUDIO.sample_width)
    if channels != _AUDIO.channels:
        pcm = audioop.tomono(pcm, _AUDIO.sample_width, 0.5, 0.5)
    if rate != _AUDIO.sample_rate:
        pcm, _state = audioop.ratecv(
            pcm, _AUDIO.sample_width, _AUDIO.channels, rate, _AUDIO.sample_rate, None
        )
    if len(pcm) % _AUDIO.sample_width != 0:
        raise ValueError(f"{path} did not convert to whole 16-bit samples")
    return pcm


async def transcribe_wav(
    path: Path,
    api_key: str,
    *,
    context: DirectSourceContext | None = None,
    handle_signals: bool = False,
) -> DirectSourceRun:
    """Run one recording through Deepgram and return the closed pipeline run."""
    pcm = pcm16_mono_16k(path)
    source = context or DirectSourceContext(
        tenant_id=1,
        session_id=0,
        source_id=path.name,
        session_user_id=None,
    )
    stt = deepgram_stt(api_key)
    run = DirectSourceRun(source, [stt])
    await run.start(handle_signals=handle_signals)
    try:
        await _wait_until_connected(stt, run)
        await _push_realtime(run, pcm + bytes(_AUDIO.frame_bytes(SILENCE_MS)))
        await _wait_for_candidates(run)
    finally:
        await run.close()
    return run


async def _wait_until_connected(stt: object, run: DirectSourceRun) -> None:
    """Wait until Deepgram's socket is up or the run has already failed."""
    deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECS
    while asyncio.get_running_loop().time() < deadline:
        if run.state is RunState.FAILED:
            raise RuntimeError(_failure_text(run))
        if getattr(stt, "_connection", None) is not None:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("Deepgram STT did not connect")


async def _push_realtime(run: DirectSourceRun, pcm: bytes) -> None:
    offset = 0
    while offset < len(pcm):
        if run.state is not RunState.RUNNING:
            return
        chunk = pcm[offset : offset + _AUDIO.frame_bytes(FRAME_MS)]
        timestamp_ms = (offset // _AUDIO.sample_width) * 1000 // _AUDIO.sample_rate
        await run.push_audio(chunk, timestamp_ms=timestamp_ms)
        duration_ms = (len(chunk) // _AUDIO.sample_width) * 1000 // _AUDIO.sample_rate
        await asyncio.sleep(duration_ms / 1000)
        offset += len(chunk)


async def _wait_for_candidates(run: DirectSourceRun) -> None:
    deadline = asyncio.get_running_loop().time() + RESULT_TIMEOUT_SECS
    while asyncio.get_running_loop().time() < deadline:
        if run.candidates or run.state is not RunState.RUNNING:
            break
        await asyncio.sleep(0.05)
    if run.candidates and run.state is RunState.RUNNING:
        await asyncio.sleep(0.3)


def _failure_text(run: DirectSourceRun) -> str:
    if not run.failures:
        return "Deepgram STT failed before it connected"
    return "; ".join(failure.message for failure in run.failures)


def main() -> None:
    """Transcribe a WAV path from the command line."""
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Transcribe a WAV with Deepgram STT. Candidates are printed, not stored."
    )
    parser.add_argument("wav_path", type=Path)
    args = parser.parse_args()
    api_key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if not api_key:
        print("DEEPGRAM_API_KEY must be configured", file=sys.stderr)
        sys.exit(2)
    try:
        run = asyncio.run(transcribe_wav(args.wav_path, api_key, handle_signals=True))
    except (OSError, TimeoutError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    for failure in run.failures:
        print(f"failure fatal={failure.fatal}: {failure.message}", file=sys.stderr)
    for candidate in run.candidates:
        print(f"[{candidate.start_ms}-{candidate.end_ms}] {candidate.text}")
    if run.state is RunState.FAILED or not run.candidates:
        sys.exit(1)


if __name__ == "__main__":
    main()
