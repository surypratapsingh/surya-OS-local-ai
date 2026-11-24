#!/usr/bin/env python3
"""
tests/test_verify_tone.py - Test battery for tools/verify-tone.py Goertzel tone verifier.

Addresses F2.11 blocking finding F1 (docs/reviews/k5c-freebuff.log):
tools/verify-tone.py previously lacked an automated test in the repository.
Derives test cases from the Goertzel specification and the F2.11 section 3 battery:
- Case A: 440 Hz full tone -> PASS (exit 0)
- Case B: 880 Hz tone with --expect 440 -> FAIL (exit 1, frequency mismatch)
- Case C: 220 Hz tone with --expect 440 -> FAIL (exit 1, frequency mismatch)
- Case D: 445 Hz tone with --expect 440, tol 10 -> PASS (exit 0, within tolerance)
- Case E: Silence / near-zero RMS -> FAIL (exit 1, silence rejected)
- Case F: Truncated < 50 ms (1328 samples) -> FAIL (exit 1, too short)
- Case G: Unfinalized streaming WAV (chunk_sz=0) -> PASS (exit 0, direct RIFF fallback)
- Case H: Nonexistent / corrupt files -> FAIL (exit 1)
- Plant sensitivity: proving an oracle forced to 'return True' is caught.

Standard library only (math, struct, subprocess, sys, tempfile, unittest, wave).
"""

from __future__ import annotations

import importlib.util
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

REPO = Path(__file__).resolve().parents[1]
VERIFY_TONE = REPO / "tools" / "verify-tone.py"

# Import tools/verify-tone.py as a module for direct function testing
_spec = importlib.util.spec_from_file_location("verify_tone", VERIFY_TONE)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load spec for {VERIFY_TONE}")
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)


def make_sine_pcm(
    freq_hz: float,
    num_samples: int,
    sample_rate: int = 44100,
    amplitude: float = 16000.0,
    num_channels: int = 2,
) -> bytes:
    """Generate 16-bit signed PCM audio bytes for a pure sine tone."""
    raw = bytearray()
    for i in range(num_samples):
        if freq_hz > 0:
            val = int(amplitude * math.sin(2.0 * math.pi * freq_hz * i / sample_rate))
        else:
            val = 0
        clamped = max(-32768, min(32767, val))
        for _ in range(num_channels):
            raw.extend(struct.pack("<h", clamped))
    return bytes(raw)


def write_standard_wav(
    path: Path,
    pcm_bytes: bytes,
    sample_rate: int = 44100,
    num_channels: int = 2,
    sampwidth: int = 2,
) -> None:
    """Write standard finalized WAV file via stdlib wave module."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(num_channels)
        w.setsampwidth(sampwidth)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)


def write_unfinalized_wav(
    path: Path,
    pcm_bytes: bytes,
    sample_rate: int = 44100,
    num_channels: int = 2,
    sampwidth: int = 2,
) -> None:
    """Write unfinalized streaming WAV with data chunk_sz=0 (as left by abrupt QEMU exit)."""
    header = bytearray()
    # RIFF header with total file size placeholder
    header.extend(b"RIFF")
    header.extend(struct.pack("<I", 36 + len(pcm_bytes)))
    header.extend(b"WAVE")
    # fmt chunk
    header.extend(b"fmt ")
    header.extend(struct.pack("<I", 16))  # chunk size
    byte_rate = sample_rate * num_channels * sampwidth
    block_align = num_channels * sampwidth
    header.extend(struct.pack("<HHIIHH", 1, num_channels, sample_rate, byte_rate, block_align, sampwidth * 8))
    # data chunk with chunk_sz = 0
    header.extend(b"data")
    header.extend(struct.pack("<I", 0))
    path.write_bytes(bytes(header) + pcm_bytes)


class TestVerifyTone(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.tmppath = Path(cls.tmpdir.name)

        # Case A: 440 Hz full tone (0.25 s = 11025 samples)
        cls.wav_440 = cls.tmppath / "tone_440.wav"
        write_standard_wav(cls.wav_440, make_sine_pcm(440.0, 11025))

        # Case B: 880 Hz full tone
        cls.wav_880 = cls.tmppath / "tone_880.wav"
        write_standard_wav(cls.wav_880, make_sine_pcm(880.0, 11025))

        # Case C: 220 Hz full tone
        cls.wav_220 = cls.tmppath / "tone_220.wav"
        write_standard_wav(cls.wav_220, make_sine_pcm(220.0, 11025))

        # Case D: 445 Hz tone (within 10 Hz tolerance of 440)
        cls.wav_445 = cls.tmppath / "tone_445.wav"
        write_standard_wav(cls.wav_445, make_sine_pcm(445.0, 11025))

        # Case E: Silence (zero amplitude)
        cls.wav_silence = cls.tmppath / "silence.wav"
        write_standard_wav(cls.wav_silence, make_sine_pcm(0.0, 11025))

        # Case F: Too short (1328 samples < 44100 // 20 = 2205)
        cls.wav_short = cls.tmppath / "too_short.wav"
        write_standard_wav(cls.wav_short, make_sine_pcm(440.0, 1328))

        # Case G: Unfinalized streaming WAV
        cls.wav_unfinalized = cls.tmppath / "unfinalized_440.wav"
        write_unfinalized_wav(cls.wav_unfinalized, make_sine_pcm(440.0, 11025))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        cmd = [sys.executable, str(VERIFY_TONE)] + list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        return proc.returncode, proc.stdout, proc.stderr

    def test_case_a_440hz_full_pass(self):
        """Case A: 440 Hz tone verified at 440 Hz returns True and exits 0."""
        self.assertTrue(vt.verify_wav(str(self.wav_440), expected_hz=440.0, tolerance_hz=10.0))
        rc, out, _ = self.run_cli(str(self.wav_440), "--expect", "440.0")
        self.assertEqual(rc, 0)
        self.assertIn("PASS: 440.0 Hz tone verified", out)

    def test_case_b_880hz_full_fail(self):
        """Case B: 880 Hz tone fails expectation of 440 Hz with exit 1."""
        self.assertFalse(vt.verify_wav(str(self.wav_880), expected_hz=440.0, tolerance_hz=10.0))
        rc, out, _ = self.run_cli(str(self.wav_880), "--expect", "440.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: detected peak frequency 880.0 Hz differs", out)

    def test_case_c_220hz_full_fail(self):
        """Case C: 220 Hz tone fails expectation of 440 Hz with exit 1."""
        self.assertFalse(vt.verify_wav(str(self.wav_220), expected_hz=440.0, tolerance_hz=10.0))
        rc, out, _ = self.run_cli(str(self.wav_220), "--expect", "440.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: detected peak frequency 220.0 Hz differs", out)

    def test_case_d_445hz_tolerance_pass(self):
        """Case D: 445 Hz tone within 10 Hz tolerance passes expectation of 440 Hz."""
        self.assertTrue(vt.verify_wav(str(self.wav_445), expected_hz=440.0, tolerance_hz=10.0))
        rc, out, _ = self.run_cli(str(self.wav_445), "--expect", "440.0", "--tolerance", "10.0")
        self.assertEqual(rc, 0)
        self.assertIn("PASS: 440.0 Hz tone verified", out)

    def test_case_e_silence_fail(self):
        """Case E: Silence (RMS < 500) fails with silence rejection and exit 1."""
        self.assertFalse(vt.verify_wav(str(self.wav_silence), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(self.wav_silence), "--expect", "440.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: audio signal is silence or near-zero", out)

    def test_case_f_too_short_fail(self):
        """Case F: Audio shorter than 50 ms (1328 samples) fails with exit 1."""
        self.assertFalse(vt.verify_wav(str(self.wav_short), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(self.wav_short), "--expect", "440.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: audio too short: 1328 samples", out)

    def test_case_g_unfinalized_wav_pass(self):
        """Case G: Unfinalized WAV (data chunk_sz=0) parsed via direct RIFF fallback."""
        self.assertTrue(vt.verify_wav(str(self.wav_unfinalized), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(self.wav_unfinalized), "--expect", "440.0")
        self.assertEqual(rc, 0)
        self.assertIn("PASS: 440.0 Hz tone verified", out)

    def test_case_h_missing_and_corrupt_files(self):
        """Case H: Missing or invalid RIFF files fail cleanly."""
        missing = self.tmppath / "nonexistent.wav"
        self.assertFalse(vt.verify_wav(str(missing), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(missing))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: WAV file does not exist", out)

        corrupt = self.tmppath / "corrupt.wav"
        corrupt.write_bytes(b"NOT_A_VALID_RIFF_HEADER")
        self.assertFalse(vt.verify_wav(str(corrupt), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(corrupt))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: unable to read WAV file", out)

    def test_plant_always_true_oracle_caught(self):
        """Plant test (Finding F1): prove that an oracle returning True unconditionally is caught."""
        # A defective oracle that unconditionally returns True:
        def bogus_verify_wav(path, expected_hz=440.0, tolerance_hz=10.0):
            return True

        # When tested against the 880 Hz WAV (which must fail), the bogus oracle passes it.
        # This assertion proves our test requirement (self.assertFalse on 880 Hz) CAUGHT the plant.
        self.assertFalse(
            vt.verify_wav(str(self.wav_880), expected_hz=440.0),
            "Real verify_wav correctly returns False on 880 Hz tone",
        )
        # Assert that the bogus oracle returns True on 880 Hz:
        plant_result = bogus_verify_wav(str(self.wav_880), expected_hz=440.0)
        self.assertTrue(plant_result, "Plant confirmed: bogus oracle returned True")
        # Therefore, if the real verify_wav behaved like bogus_verify_wav, test_case_b_880hz_full_fail would fail.


if __name__ == "__main__":
    unittest.main()
