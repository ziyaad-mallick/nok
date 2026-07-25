"""Replay fixture WAVs (or synthetic tracks) through detector + IOI.

Reports, in accelerated time (no real-time waits):
  * double-taps detected out of 20 in taps.wav
  * false wakes in typing.wav and bumps.wav
  * rolling-max per-block compute time (HARD FAIL if > 32ms, Amendment 9)

Exit code reflects the Day-1 kill criterion: PASS iff >= 18/20 double-taps
detected AND 0 false wakes (typing + bumps) AND compute budget met.

If a fixture WAV is missing (they don't exist until scripts/record_fixtures.py
runs), that track falls back to a numpy-synthesized stand-in, so the harness --
and its tests -- work before any real recording exists. Force synthetic with
`--synthetic`.

    python -m scripts.replay_harness            # real fixtures if present
    python -m scripts.replay_harness --synthetic
"""
import argparse
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nok.detector import DetectorConfig, KnockDetector  # noqa: E402
from nok.ioi import DOUBLE_TAP, TRIPLE_TAP, IOIStateMachine  # noqa: E402
from nok.ring_buffer import SAMPLE_RATE  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"
N_PAIRS = 20
BUDGET_MS = 32.0
KILL_MIN_DETECTED = 18


# --------------------------------------------------------------------------
# WAV I/O (stdlib wave + numpy, 16kHz mono, float32 <-> int16)
# --------------------------------------------------------------------------

def write_wav(path, samples, sr: int = SAMPLE_RATE) -> None:
    data = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    ints = (data * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(ints.tobytes())


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return arr, sr


# --------------------------------------------------------------------------
# Synthetic signal generators (single source of truth; imported by tests too)
# --------------------------------------------------------------------------

def _one_pole_lowpass(x, sr, cutoff):
    """Cheap RC one-pole low-pass (numpy loop; only used on short events)."""
    dt = 1.0 / sr
    rc = 1.0 / (2.0 * np.pi * cutoff)
    a = dt / (rc + dt)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(x.shape[0]):
        acc += a * (x[i] - acc)
        y[i] = acc
    return y


def synth_silence(n, noise=1e-4, seed=None):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * noise).astype(np.float32)


def synth_knock(sr=SAMPLE_RATE, dur=0.06, cutoff=600.0, amp=0.6, seed=None):
    """Broadband LOW-frequency thump with a sharp attack: low-passed white
    noise under a fast exponential-decay envelope."""
    n = int(dur * sr)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n).astype(np.float32)
    # Two one-pole stages (-12 dB/oct) keep the thump genuinely low-frequency.
    low = _one_pole_lowpass(_one_pole_lowpass(noise, sr, cutoff), sr, cutoff)
    t = np.arange(n) / sr
    env = np.exp(-t / 0.015).astype(np.float32)  # ~15ms decay, instant attack
    y = low * env
    peak = float(np.max(np.abs(y))) or 1.0
    return (y / peak * amp).astype(np.float32)


def synth_typing_click(sr=SAMPLE_RATE, dur=0.008, freq=5000.0, amp=0.35):
    """Short HIGH-centroid click: damped high-frequency sinusoid."""
    n = int(dur * sr)
    t = np.arange(n) / sr
    y = np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.002)
    return (y * amp).astype(np.float32)


def synth_tone(sr=SAMPLE_RATE, dur=1.0, freqs=(300.0, 600.0), amp=0.25, fade=0.4):
    """Sustained music-ish tone with a gradual fade-in (no sharp attack)."""
    n = int(dur * sr)
    t = np.arange(n) / sr
    y = np.zeros(n, dtype=np.float32)
    for f in freqs:
        y += np.sin(2 * np.pi * f * t).astype(np.float32)
    y /= len(freqs)
    nf = int(fade * sr)
    if nf > 0:
        ramp = np.linspace(0.0, 1.0, min(nf, n)).astype(np.float32)
        y[:ramp.shape[0]] *= ramp
    return (y * amp).astype(np.float32)


def _place(track, event, start):
    end = min(start + event.shape[0], track.shape[0])
    track[start:end] += event[:end - start]


def synth_taps_track(sr=SAMPLE_RATE, n_pairs=N_PAIRS, within=0.2, between=1.5,
                     lead=0.3, seed=0):
    """n_pairs deliberate double-taps on a quiet noise floor."""
    total = int((lead + n_pairs * between + 1.0) * sr)
    track = synth_silence(total, seed=seed)
    pos = lead
    for k in range(n_pairs):
        _place(track, synth_knock(sr, seed=seed + 2 * k), int(pos * sr))
        _place(track, synth_knock(sr, seed=seed + 2 * k + 1), int((pos + within) * sr))
        pos += between
    return track


def synth_typing_track(sr=SAMPLE_RATE, seconds=8.0, rate=8.0, lead=0.3, seed=1):
    """Continuous typing clicks (high centroid) -- should never wake."""
    total = int((lead + seconds) * sr)
    track = synth_silence(total, seed=seed)
    step = 1.0 / rate
    pos = lead
    while pos < lead + seconds:
        _place(track, synth_typing_click(sr), int(pos * sr))
        pos += step
    return track


def synth_bumps_track(sr=SAMPLE_RATE, n_bumps=10, spacing=2.0, lead=0.3, seed=2):
    """Isolated desk bumps -- knock-like thumps but far apart, so each is a
    single onset that the IOI machine resolves to NONE (no false wake)."""
    total = int((lead + n_bumps * spacing + 1.0) * sr)
    track = synth_silence(total, seed=seed)
    pos = lead
    for k in range(n_bumps):
        _place(track, synth_knock(sr, amp=0.5, seed=seed + 100 + k), int(pos * sr))
        pos += spacing
    return track


# --------------------------------------------------------------------------
# Replay engine
# --------------------------------------------------------------------------

@dataclass
class KillResult:
    double_taps: int
    false_wakes_typing: int
    false_wakes_bumps: int
    max_block_ms: float
    compute_ok: bool
    passed: bool


def replay_track(samples, sr, config=None):
    """Run one track through detector + IOI. Returns (wake_count, max_ms)."""
    det = KnockDetector(config or DetectorConfig(samplerate=sr))
    machine = IOIStateMachine()
    bs = det.config.blocksize
    n_blocks = samples.shape[0] // bs
    wakes = 0
    now = 0.0
    for i in range(n_blocks):
        block = samples[i * bs:(i + 1) * bs]
        now = (i + 1) * bs / sr
        onset = det.process_block(block, now)
        if onset is not None:
            if machine.feed(onset) in (DOUBLE_TAP, TRIPLE_TAP):
                wakes += 1
        if machine.poll(now) in (DOUBLE_TAP, TRIPLE_TAP):
            wakes += 1
    # Flush a trailing chain that never saw following silence.
    if machine.poll(now + 10.0) in (DOUBLE_TAP, TRIPLE_TAP):
        wakes += 1
    return wakes, det.max_block_seconds * 1000.0


def run_kill_criterion(taps, typing, bumps, sr, config=None) -> KillResult:
    detected, ms_t = replay_track(taps, sr, config)
    fw_typing, ms_y = replay_track(typing, sr, config)
    fw_bumps, ms_b = replay_track(bumps, sr, config)
    max_ms = max(ms_t, ms_y, ms_b)
    compute_ok = max_ms <= BUDGET_MS
    passed = detected >= KILL_MIN_DETECTED and fw_typing == 0 and fw_bumps == 0 and compute_ok
    return KillResult(detected, fw_typing, fw_bumps, max_ms, compute_ok, passed)


# --------------------------------------------------------------------------
# Fixture loading + CLI
# --------------------------------------------------------------------------

def _load(name, synth_fn, sr, force_synth):
    path = FIXTURES_DIR / name
    if not force_synth and path.exists():
        arr, wav_sr = read_wav(path)
        return arr, wav_sr, "real"
    return synth_fn(sr), sr, "synthetic"


def main():
    ap = argparse.ArgumentParser(description="Replay fixtures through detector+IOI.")
    ap.add_argument("--synthetic", action="store_true",
                    help="ignore fixture WAVs and use synthesized tracks")
    args = ap.parse_args()
    sr = SAMPLE_RATE

    taps, sr_t, src_t = _load("taps.wav", synth_taps_track, sr, args.synthetic)
    typing, _, src_y = _load("typing.wav", synth_typing_track, sr, args.synthetic)
    bumps, _, src_b = _load("bumps.wav", synth_bumps_track, sr, args.synthetic)

    res = run_kill_criterion(taps, typing, bumps, sr_t)

    print("=== Nok detector replay harness ===")
    print(f"sources: taps={src_t} typing={src_y} bumps={src_b}")
    print(f"double-taps detected : {res.double_taps}/{N_PAIRS}"
          f"  (need >= {KILL_MIN_DETECTED})")
    print(f"false wakes (typing) : {res.false_wakes_typing}  (need 0)")
    print(f"false wakes (bumps)  : {res.false_wakes_bumps}  (need 0)")
    print(f"max block compute    : {res.max_block_ms:.3f} ms"
          f"  (budget {BUDGET_MS:.0f} ms) -> {'OK' if res.compute_ok else 'FAIL'}")
    print()
    print(f"KILL CRITERION: {'PASS' if res.passed else 'FAIL'}")
    sys.exit(0 if res.passed else 1)


if __name__ == "__main__":
    main()
