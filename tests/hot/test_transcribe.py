"""Tests for prerecorded audio normalization before Deepgram."""

import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.hot.transcribe import pcm16_mono_16k


def _write_wav(
    path: Path,
    *,
    rate: int,
    channels: int,
    width: int,
    frames: bytes,
) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(frames)


class PcmNormalizationTest(unittest.TestCase):
    """Verify WAV input is converted to the STT frame format."""

    def test_matching_wav_is_returned_unchanged(self) -> None:
        pcm = b"\x01\x00\xff\x7f" * 8
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            _write_wav(path, rate=16000, channels=1, width=2, frames=pcm)

            self.assertEqual(pcm16_mono_16k(path), pcm)

    def test_stereo_8bit_audio_becomes_16k_mono_16bit(self) -> None:
        frames = bytes([0, 200]) * 800
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            _write_wav(path, rate=8000, channels=2, width=1, frames=frames)

            pcm = pcm16_mono_16k(path)

        self.assertEqual(len(pcm) % 2, 0)
        self.assertGreater(len(pcm), 3000)
        self.assertLess(len(pcm), 3400)
        self.assertTrue(any(pcm))
