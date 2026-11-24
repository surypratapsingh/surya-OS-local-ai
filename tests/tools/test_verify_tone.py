"""Unit tests for tools/verify-tone.py (stdlib unittest only).

Oracle: the test writes its own WAV files with known tone frequencies. The
expected frequency is the frequency the test put in, not a value read back
from the tool. Each test name says which tool behaviour it pins.
"""

import importlib.util
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "verify-tone.py"))

_spec = importlib.util.spec_from_file_location("verify_tone", TOOL)
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)

RATE = 8000


def write_tone_wav(path, freq, seconds=1.0, amp=8000, rate=RATE):
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n)
    )
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return n


def write_silence_wav(path, seconds=1.0):
    n = int(RATE * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00\x00" * n)


class GoertzelTests(unittest.TestCase):
    def test_power_peaks_at_tone_frequency(self):
        # Pure 440 Hz tone: power at 440 must dominate power at 1000 Hz.
        path = os.path.join(TMP, "g440.wav")
        write_tone_wav(path, 440.0)
        samples, rate = vt.load_pcm_samples(path)
        on = vt.goertzel(samples, rate, 440.0)
        off = vt.goertzel(samples, rate, 1000.0)
        self.assertGreater(on, 1000.0 * off)

    def test_empty_input_returns_zero(self):
        self.assertEqual(vt.goertzel([], RATE, 440.0), 0.0)


class VerifyWavTests(unittest.TestCase):
    def test_correct_tone_passes(self):
        path = os.path.join(TMP, "t440.wav")
        write_tone_wav(path, 440.0)
        self.assertTrue(vt.verify_wav(path, 440.0, 10.0))

    def test_wrong_tone_fails(self):
        # Tone is 880 Hz, expected 440 Hz: must be rejected.
        path = os.path.join(TMP, "t880.wav")
        write_tone_wav(path, 880.0)
        self.assertFalse(vt.verify_wav(path, 440.0, 10.0))

    def test_silence_is_rejected(self):
        path = os.path.join(TMP, "silence.wav")
        write_silence_wav(path)
        self.assertFalse(vt.verify_wav(path, 440.0, 10.0))

    def test_loud_off_frequency_tone_is_rejected_by_frequency_gate(self):
        # Two pure tones: 440 Hz (amp 8000) is stronger than 1000 Hz (amp 7000).
        # Peak detection picks 440 Hz, which is 560 Hz from the expected 1000 Hz.
        # The SNR gate passes here (ambient grid does not hit either tone), so
        # only the frequency-tolerance gate can reject it.
        path = os.path.join(TMP, "two_tones.wav")
        n = RATE
        frames = b"".join(
            struct.pack(
                "<h",
                int(8000 * math.sin(2 * math.pi * 440 * i / RATE)
                    + 7000 * math.sin(2 * math.pi * 1000 * i / RATE)),
            )
            for i in range(n)
        )
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(frames)
        self.assertFalse(vt.verify_wav(path, 1000.0, 10.0))

    def test_quiet_correct_tone_is_rejected_by_rms_gate(self):
        # 440 Hz at amplitude 100 gives RMS about 71, below the 500 gate.
        # Frequency and SNR both pass, so only the RMS gate can reject it.
        path = os.path.join(TMP, "quiet440.wav")
        write_tone_wav(path, 440.0, amp=100)
        self.assertFalse(vt.verify_wav(path, 440.0, 10.0))

    def test_correct_peak_below_snr_gate_is_rejected(self):
        # 440 Hz at amplitude 3000 is the strongest bin, so the frequency gate
        # passes. Every other grid bin used for the ambient average (integer
        # Hz, 50 Hz grid, outside 390-490 Hz) carries amplitude 1000, so the
        # SNR is (3000/1000)**2 = 9 (9.5 dB), below the 15 dB gate.
        path = os.path.join(TMP, "lowsnr.wav")
        ambient = [f for f in range(100, 2001, 50) if abs(f - 440) > 50]
        frames = b"".join(
            struct.pack(
                "<h",
                int(3000 * math.sin(2 * math.pi * 440 * i / RATE)
                    + sum(1000 * math.sin(2 * math.pi * f * i / RATE) for f in ambient)),
            )
            for i in range(RATE)
        )
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(frames)
        self.assertFalse(vt.verify_wav(path, 440.0, 10.0))

    def test_too_short_is_rejected(self):
        # 20 ms of tone is below the 50 ms minimum (rate // 20 samples).
        path = os.path.join(TMP, "short.wav")
        write_tone_wav(path, 440.0, seconds=0.02)
        self.assertFalse(vt.verify_wav(path, 440.0, 10.0))

    def test_missing_file_is_rejected(self):
        self.assertFalse(vt.verify_wav(os.path.join(TMP, "nope.wav")))

    def test_not_riff_is_rejected(self):
        path = os.path.join(TMP, "junk.wav")
        with open(path, "wb") as f:
            f.write(b"X" * 200)
        self.assertFalse(vt.verify_wav(path, 440.0, 10.0))


class UnfinalizedWavTests(unittest.TestCase):
    def test_zero_length_data_chunk_loads_all_samples(self):
        # QEMU exit leaves the data chunk size at 0. The fallback parser must
        # read to end of file. Expected count = samples the test wrote.
        n = int(RATE * 0.5)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / RATE)))
            for i in range(n)
        )
        fmt = struct.pack("<HHIIHH", 1, 1, RATE, RATE * 2, 2, 16)
        header = (
            b"RIFF" + struct.pack("<I", 0) + b"WAVE"
            + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", 0)
        )
        path = os.path.join(TMP, "unfinal.wav")
        with open(path, "wb") as f:
            f.write(header + frames)
        samples, rate = vt.load_pcm_samples(path)
        self.assertEqual(rate, RATE)
        self.assertEqual(len(samples), n)


class CliTests(unittest.TestCase):
    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, TOOL, *args], capture_output=True, text=True
        ).returncode

    def test_exit_0_on_match(self):
        path = os.path.join(TMP, "cli440.wav")
        write_tone_wav(path, 440.0)
        self.assertEqual(self.run_tool(path, "--expect", "440"), 0)

    def test_exit_1_on_mismatch(self):
        path = os.path.join(TMP, "cli880.wav")
        write_tone_wav(path, 880.0)
        self.assertEqual(self.run_tool(path, "--expect", "440"), 1)


def setUpModule():
    global TMP
    TMP = tempfile.mkdtemp(prefix="verify-tone-test-")


if __name__ == "__main__":
    unittest.main()
