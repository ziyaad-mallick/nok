"""Knock-detector tests on numpy-synthesized audio (no real recordings).

Signal generators live in scripts.replay_harness (single source of truth,
shared with the harness itself).
"""
import numpy as np

from nok.detector import BLOCKSIZE, DetectorConfig, KnockDetector
from nok.ioi import DOUBLE_TAP, TRIPLE_TAP, IOIStateMachine
from nok.ring_buffer import SAMPLE_RATE
from scripts.replay_harness import (
    synth_knock,
    synth_silence,
    synth_tone,
    synth_typing_click,
    _place,
)

SR = SAMPLE_RATE


def _run(detector, samples, sr=SR):
    """Feed a sample array block-by-block; return list of onset timestamps."""
    bs = detector.config.blocksize
    onsets = []
    for i in range(samples.shape[0] // bs):
        block = samples[i * bs:(i + 1) * bs]
        now = (i + 1) * bs / sr
        t = detector.process_block(block, now)
        if t is not None:
            onsets.append(t)
    return onsets


def _wakes(samples, sr=SR, config=None):
    det = KnockDetector(config or DetectorConfig(samplerate=sr))
    machine = IOIStateMachine()
    bs = det.config.blocksize
    wakes = []
    now = 0.0
    for i in range(samples.shape[0] // bs):
        block = samples[i * bs:(i + 1) * bs]
        now = (i + 1) * bs / sr
        t = det.process_block(block, now)
        if t is not None:
            d = machine.feed(t)
            if d in (DOUBLE_TAP, TRIPLE_TAP):
                wakes.append(d)
        d = machine.poll(now)
        if d in (DOUBLE_TAP, TRIPLE_TAP):
            wakes.append(d)
    d = machine.poll(now + 10.0)
    if d in (DOUBLE_TAP, TRIPLE_TAP):
        wakes.append(d)
    return wakes


def _track(*, lead=0.3, tail=0.3):
    """Silent base long enough to prime warmup, with head/tail padding."""
    n = int((lead + tail) * SR)
    return synth_silence(n, seed=7), int(lead * SR)


# -- detection -------------------------------------------------------------

def test_single_knock_detected():
    base, at = _track()
    _place(base, synth_knock(SR, seed=1), at)
    onsets = _run(KnockDetector(), base)
    assert len(onsets) == 1


def test_typing_clicks_rejected():
    base = synth_silence(int(2.0 * SR), seed=3)
    pos = 0.3
    while pos < 1.7:
        _place(base, synth_typing_click(SR), int(pos * SR))
        pos += 0.12
    assert _run(KnockDetector(), base) == []


def test_silence_rejected():
    base = synth_silence(int(2.0 * SR), seed=4)
    assert _run(KnockDetector(), base) == []


def test_sustained_tone_rejected():
    base = synth_silence(int(2.0 * SR), seed=5)
    # abrupt low tone: one initial transient, then steady -> no further onsets
    _place(base, synth_tone(SR, dur=1.5, fade=0.0), int(0.3 * SR))
    onsets = _run(KnockDetector(), base)
    # a sustained tone must not produce a rhythmic pair -> at most one onset
    assert len(onsets) <= 1
    assert _wakes(base) == []


def test_double_tap_wakes():
    base = synth_silence(int(1.5 * SR), seed=8)
    _place(base, synth_knock(SR, seed=1), int(0.3 * SR))
    _place(base, synth_knock(SR, seed=2), int(0.5 * SR))  # 200ms later
    assert _wakes(base) == [DOUBLE_TAP]


def test_typing_and_silence_never_wake():
    base = synth_silence(int(2.0 * SR), seed=9)
    pos = 0.3
    while pos < 1.7:
        _place(base, synth_typing_click(SR), int(pos * SR))
        pos += 0.13
    assert _wakes(base) == []


# -- gating + cooldown -----------------------------------------------------

def test_playback_gate_drops_onset():
    det = KnockDetector()
    base, at = _track()
    _place(base, synth_knock(SR, seed=1), at)
    det.playback_gate = True
    assert _run(det, base) == []  # gated the whole time -> dropped


def test_busy_gate_drops_onset():
    det = KnockDetector()
    base, at = _track()
    _place(base, synth_knock(SR, seed=1), at)
    det.busy_gate = True
    assert _run(det, base) == []


def test_cooldown_drops_onset_after_gate_released():
    cfg = DetectorConfig(cooldown_s=0.5)
    det = KnockDetector(cfg)
    bs = BLOCKSIZE

    # Timeline: gate active until 0.5s. Knock A@0.3s gated (dropped); knock
    # B@0.8s is 0.3s after release -> inside 0.5s cooldown (dropped); knock
    # C@1.3s is 0.8s after release -> outside cooldown (kept).
    total = synth_silence(int(2.0 * SR), seed=10)
    _place(total, synth_knock(SR, seed=1), int(0.3 * SR))
    _place(total, synth_knock(SR, seed=2), int(0.8 * SR))
    _place(total, synth_knock(SR, seed=3), int(1.3 * SR))

    kept = []
    for i in range(total.shape[0] // bs):
        now = (i + 1) * bs / SR
        det.playback_gate = now < 0.5  # gate active only across knock A
        t = det.process_block(total[i * bs:(i + 1) * bs], now)
        if t is not None:
            kept.append(t)

    assert len(kept) == 1
    assert kept[0] > 1.0


# -- SIMPLE thump mode -----------------------------------------------------

def test_simple_mode_wakes_on_loud_spike():
    cfg = DetectorConfig(simple_mode=True, simple_amplitude=0.2)
    det = KnockDetector(cfg)
    base, at = _track()
    spike = np.zeros(int(0.02 * SR), dtype=np.float32)
    spike[0] = 0.5  # single loud sample
    _place(base, spike, at)
    assert len(_run(det, base)) >= 1


def test_simple_mode_ignores_quiet_signal():
    cfg = DetectorConfig(simple_mode=True, simple_amplitude=0.2)
    det = KnockDetector(cfg)
    base = synth_silence(int(1.0 * SR), seed=11)
    _place(base, synth_typing_click(SR, amp=0.1), int(0.3 * SR))  # below floor
    assert _run(det, base) == []


def test_simple_mode_wakes_on_high_centroid_spike():
    # classifier bypassed: even a high-frequency loud click wakes in thump mode
    cfg = DetectorConfig(simple_mode=True, simple_amplitude=0.2)
    det = KnockDetector(cfg)
    base, at = _track()
    _place(base, synth_typing_click(SR, amp=0.5), at)
    assert len(_run(det, base)) >= 1


# -- compute budget --------------------------------------------------------

def test_consumed_drains_backlog_after_a_wake():
    from nok.detector import _consumed_after_chunk
    sr = 16000
    # normal fast chunk: buffer barely moved -> advance to where we started
    assert _consumed_after_chunk(1000, 1000, sr) == 1000
    assert _consumed_after_chunk(1000, 1000 + 512, sr) == 1000  # ~1 block jitter
    # a wake blocked us for seconds: discard the backlog captured meanwhile
    blocked = 1000 + int(2.0 * sr)
    assert _consumed_after_chunk(1000, blocked, sr) == blocked


def test_per_block_compute_under_budget():
    det = KnockDetector()
    base = synth_silence(int(2.0 * SR), seed=12)
    _place(base, synth_knock(SR, seed=1), int(0.5 * SR))
    _run(det, base)
    assert det.max_block_seconds * 1000.0 < 32.0
