#!/usr/bin/env python3
"""
tools/verify-tone.py - Goertzel filter audio frequency verifier for NOVA.

Derived from Goertzel algorithm specification (Goertzel, G. "An Algorithm for the
Evaluation of Finite Trigonometric Series", Amer. Math. Monthly, 1958).
Stdlib-only (wave, struct, math, sys, os).

Measures dominant frequency of 16-bit PCM WAV recorded from QEMU Intel HDA
and asserts that the detected peak matches the expected frequency (e.g. 440 Hz).
Rejects silence, off-frequency tones, and harmonic/subharmonic misdetections.
Handles both finalized WAVs and streaming WAVs left unfinalized by QEMU exit.
"""

import argparse
import math
import os
import struct
import sys
import wave


def goertzel(samples, sample_rate, target_freq):
    """
    Compute Goertzel filter squared magnitude response at target_freq.
    Standard second-order IIR recursion.
    """
    n = len(samples)
    if n == 0:
        return 0.0
    w = 2.0 * math.pi * target_freq / sample_rate
    coeff = 2.0 * math.cos(w)
    s0 = 0.0
    s1 = 0.0
    s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2 = s1
        s1 = s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return power


def load_pcm_samples(path):
    """
    Load 16-bit PCM samples from a WAV file.
    Tries stdlib wave module first; falls back to direct RIFF parsing
    for unfinalized streaming WAV files produced when QEMU exits abruptly.
    """
    try:
        with wave.open(path, "rb") as w:
            nchannels = w.getnchannels()
            sampwidth = w.getsampwidth()
            rate = w.getframerate()
            nframes = w.getnframes()
            raw = w.readframes(nframes)
            if sampwidth == 2 and nframes > 0:
                step = 2 * nchannels
                samples = [
                    float(struct.unpack_from("<h", raw, i)[0])
                    for i in range(0, len(raw), step)
                ]
                return samples, rate
    except Exception:
        pass

    # Direct RIFF parser fallback
    with open(path, "rb") as f:
        data = f.read()

    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"Not a valid RIFF/WAVE file ({path})")

    idx = 12
    rate = 44100
    nchannels = 2
    sampwidth = 2
    raw_pcm = b""

    while idx + 8 <= len(data):
        chunk_id = data[idx : idx + 4]
        chunk_sz = struct.unpack_from("<I", data, idx + 4)[0]
        idx += 8
        if chunk_id == b"fmt ":
            audio_format, nchannels, rate, _, _, bits = struct.unpack_from(
                "<HHIIHH", data, idx
            )
            sampwidth = bits // 8
            idx += chunk_sz
        elif chunk_id == b"data":
            if chunk_sz == 0 or idx + chunk_sz > len(data):
                # Unfinalized data chunk (e.g. from QEMU exit)
                raw_pcm = data[idx:]
                idx = len(data)
            else:
                raw_pcm = data[idx : idx + chunk_sz]
                idx += chunk_sz
        else:
            idx += chunk_sz

    if sampwidth != 2:
        raise ValueError(f"Expected 16-bit PCM, got {sampwidth * 8}-bit")

    if len(raw_pcm) < 4:
        raise ValueError("No PCM audio data found in WAV file")

    step = 2 * nchannels
    samples = [
        float(struct.unpack_from("<h", raw_pcm, i)[0])
        for i in range(0, len(raw_pcm) - (step - 1), step)
    ]
    return samples, rate


def verify_wav(path, expected_hz=440.0, tolerance_hz=10.0):
    if not os.path.exists(path):
        print(f"FAIL: WAV file does not exist: {path}")
        return False

    try:
        samples, rate = load_pcm_samples(path)
    except Exception as e:
        print(f"FAIL: unable to read WAV file {path}: {e}")
        return False

    if len(samples) < (rate // 20):  # At least 50 ms of audio
        print(f"FAIL: audio too short: {len(samples)} samples at {rate} Hz")
        return False

    # Check signal energy (reject silence / zero amplitude)
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / len(samples))
    if rms < 500.0:  # 500 out of 32767 is ~ -36 dBFS
        print(f"FAIL: audio signal is silence or near-zero (RMS = {rms:.1f})")
        return False

    # Coarse sweep from 100 Hz to 2000 Hz in 20 Hz steps
    best_f = 100.0
    best_p = -1.0
    for f in range(100, 2001, 20):
        p = goertzel(samples, rate, float(f))
        if p > best_p:
            best_p = p
            best_f = float(f)

    # Fine sweep around coarse peak (+- 30 Hz in 1 Hz steps)
    fine_start = max(50, int(best_f) - 30)
    fine_end = int(best_f) + 31
    peak_f = best_f
    peak_p = best_p
    for f in range(fine_start, fine_end):
        p = goertzel(samples, rate, float(f))
        if p > peak_p:
            peak_p = p
            peak_f = float(f)

    target_power = goertzel(samples, rate, expected_hz)

    # Compute ambient noise average (excluding region around expected_hz)
    ambient_powers = []
    for f in range(100, 2001, 50):
        if abs(f - expected_hz) > 50.0:
            ambient_powers.append(goertzel(samples, rate, float(f)))
    avg_ambient = sum(ambient_powers) / len(ambient_powers) if ambient_powers else 1.0
    if avg_ambient <= 0.0:
        avg_ambient = 1.0

    snr_ratio = target_power / avg_ambient
    snr_db = 10.0 * math.log10(snr_ratio) if snr_ratio > 0 else 0.0

    print(
        f"verify-tone: target={expected_hz:.1f} Hz, detected={peak_f:.1f} Hz, "
        f"target_power={target_power:.2e}, peak_power={peak_p:.2e}, "
        f"SNR={snr_db:.1f} dB, RMS={rms:.1f}, rate={rate} Hz, samples={len(samples)}"
    )

    # Assertions
    freq_err = abs(peak_f - expected_hz)
    if freq_err > tolerance_hz:
        print(
            f"FAIL: detected peak frequency {peak_f:.1f} Hz differs from "
            f"expected {expected_hz:.1f} Hz by {freq_err:.1f} Hz (tolerance: {tolerance_hz:.1f} Hz)"
        )
        return False

    if snr_db < 15.0:
        print(f"FAIL: signal-to-noise ratio {snr_db:.1f} dB is below 15 dB threshold")
        return False

    print(f"PASS: {expected_hz:.1f} Hz tone verified by Goertzel filter")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Verify frequency of 16-bit PCM WAV using Goertzel filter"
    )
    parser.add_argument("wav", help="Path to WAV file")
    parser.add_argument(
        "--expect",
        type=float,
        default=440.0,
        help="Expected frequency in Hz (default: 440.0)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=10.0,
        help="Tolerance in Hz (default: 10.0)",
    )
    args = parser.parse_args()

    ok = verify_wav(args.wav, args.expect, args.tolerance)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
