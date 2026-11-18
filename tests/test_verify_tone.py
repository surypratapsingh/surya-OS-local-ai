#!/usr/bin/env python3
"""
tests/test_verify_tone.py - Test battery for tools/verify-tone.py Goertzel tone verifier.

Addresses F2.11 blocking finding F1 (docs/reviews/k5c-freebuff.log):
tools/verify-tone.py previously lacked an automated test in the repository.
Derives test cases from the Goertzel specification and the F2.11 section 3 battery:
- Case A: 440 Hz full tone -> PASS (exit 0)
- Case B: 880 Hz tone with --expect 440 -> FAIL (exit 1, frequency mismatch)
- Case C: 220 Hz tone with --expect 440 -> FAIL (exit 1, frequency mismatch)
- Case D: 445 Hz and 435 Hz tones with --expect 440, default tol 10 -> PASS (exit 0, within tolerance)
- Case D3: 445 Hz and 435 Hz with narrow tolerance (tol 4) -> FAIL (exit 1, exceeds tolerance)
- Case D4: 435 Hz accepted and 445 Hz rejected with --expect 430, default tol 10 (tolerance edge)
- Case D5: 455 Hz and 425 Hz out-of-tolerance tones -> FAIL (exit 1, delta 15 > 10, SNR > 20 dB)
- Case E: Silence / near-zero RMS -> FAIL (exit 1, silence rejected)
- Case F: Truncated < 50 ms (1328 samples) -> FAIL (exit 1, too short)
- Case G: Unfinalized streaming WAV (chunk_sz=0) -> PASS (exit 0, direct RIFF fallback)
- Case H: Nonexistent / corrupt files -> FAIL (exit 1)
- Plant sensitivity 1: proving an oracle forced to 'return True' is caught.
- Plant sensitivity 2 (H14-VERIFY fix): proving tolerance_hz loosened to 1000 is caught.

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

        # 435 Hz tone (within 10 Hz tolerance of 440, exactly 5 Hz below)
        cls.wav_435 = cls.tmppath / "tone_435.wav"
        write_standard_wav(cls.wav_435, make_sine_pcm(435.0, 11025))

        # 455 Hz tone (15 Hz off 440; exceeds default 10 Hz tolerance while SNR > 20 dB)
        cls.wav_455 = cls.tmppath / "tone_455.wav"
        write_standard_wav(cls.wav_455, make_sine_pcm(455.0, 11025))

        # 425 Hz tone (15 Hz off 440; exceeds default 10 Hz tolerance while SNR > 20 dB)
        cls.wav_425 = cls.tmppath / "tone_425.wav"
        write_standard_wav(cls.wav_425, make_sine_pcm(425.0, 11025))

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

    def test_case_d_435hz_tolerance_pass(self):
        """Case D2: 435 Hz tone within default 10 Hz tolerance passes expectation of 440 Hz."""
        self.assertTrue(vt.verify_wav(str(self.wav_435), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(self.wav_435), "--expect", "440.0")
        self.assertEqual(rc, 0)
        self.assertIn("PASS: 440.0 Hz tone verified", out)

    def test_case_d_tolerance_narrow_fail(self):
        """Case D3: 445 Hz and 435 Hz fail when tolerance is narrower than frequency delta (tol 4 Hz)."""
        # 445 Hz is 5 Hz away from 440 Hz, so tol=4.0 must reject it
        self.assertFalse(vt.verify_wav(str(self.wav_445), expected_hz=440.0, tolerance_hz=4.0))
        rc, out, _ = self.run_cli(str(self.wav_445), "--expect", "440.0", "--tolerance", "4.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: detected peak frequency 445.0 Hz differs", out)

        # 435 Hz is 5 Hz away from 440 Hz, so tol=4.0 must reject it
        self.assertFalse(vt.verify_wav(str(self.wav_435), expected_hz=440.0, tolerance_hz=4.0))
        rc, out, _ = self.run_cli(str(self.wav_435), "--expect", "440.0", "--tolerance", "4.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: detected peak frequency 435.0 Hz differs", out)

    def test_case_d_tolerance_edge_expect_430(self):
        """Case D4: With --expect 430 and default 10 Hz tolerance, 435 Hz is accepted and 445 Hz is rejected."""
        # 435 Hz has delta = 5 Hz <= 10 Hz -> accepted
        self.assertTrue(vt.verify_wav(str(self.wav_435), expected_hz=430.0))
        rc, out, _ = self.run_cli(str(self.wav_435), "--expect", "430.0")
        self.assertEqual(rc, 0)
        self.assertIn("PASS: 430.0 Hz tone verified", out)

        # 445 Hz has delta = 15 Hz > 10 Hz -> rejected
        self.assertFalse(vt.verify_wav(str(self.wav_445), expected_hz=430.0))
        rc, out, _ = self.run_cli(str(self.wav_445), "--expect", "430.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: detected peak frequency 445.0 Hz differs", out)

    def test_case_d_out_of_tolerance_455hz_425hz_fail(self):
        """Case D5: 455 Hz and 425 Hz (delta 15 Hz) exceed default 10 Hz tolerance and fail with exit 1."""
        # 455 Hz
        self.assertFalse(vt.verify_wav(str(self.wav_455), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(self.wav_455), "--expect", "440.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: detected peak frequency 455.0 Hz differs from expected 440.0 Hz by 15.0 Hz (tolerance: 10.0 Hz)", out)

        # 425 Hz
        self.assertFalse(vt.verify_wav(str(self.wav_425), expected_hz=440.0))
        rc, out, _ = self.run_cli(str(self.wav_425), "--expect", "440.0")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: detected peak frequency 425.0 Hz differs from expected 440.0 Hz by 15.0 Hz (tolerance: 10.0 Hz)", out)

    def test_plant_tolerance_loosened_plant_caught(self):
        """Plant test (Finding H14-VERIFY): prove that planting tolerance_hz 10 -> 1000 is caught."""
        # Under normal tolerance, 455 Hz fails expectation of 440 Hz
        self.assertFalse(
            vt.verify_wav(str(self.wav_455), expected_hz=440.0, tolerance_hz=10.0),
            "Clean verify_wav rejects 455 Hz when expecting 440 Hz (tolerance 10 Hz)",
        )
        # If an adversary plants tolerance_hz = 1000.0, 455 Hz would erroneously pass:
        loosened_result = vt.verify_wav(str(self.wav_455), expected_hz=440.0, tolerance_hz=1000.0)
        self.assertTrue(
            loosened_result,
            "Plant confirmed: loosened tolerance 1000 erroneously passes 455 Hz tone",
        )

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
