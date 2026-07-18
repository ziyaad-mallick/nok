"""Threshold-computation unit tests on synthetic recorded arrays.

No interactive parts are exercised -- only the pure math (analyze_recording /
compute_thresholds). The acceptance test is end-to-end: thresholds derived by
calibration must, when loaded into the detector, accept taps and reject typing
and silence.
"""
import numpy as np

from miko.calibrate import analyze_recording, compute_thresholds
from miko.detector import DetectorConfig, KnockDetector
from miko.ring_buffer import SAMPLE_RATE
from scripts.replay_harness import (
    synth_knock,
    synth_silence,
    synth_typing_click,
    _place,
)

SR = SAMPLE_RATE
BS = 512


def _tap_recording(seed=0):
    rec = synth_silence(int(10.0 * SR), seed=seed)
    for k in range(10):
        _place(rec, synth_knock(SR, seed=100 + k), int((0.5 + k * 0.9) * SR))
    return rec


def _typing_recording(seed=1):
    rec = synth_silence(int(10.0 * SR), seed=seed)
    pos = 0.5
    while pos < 9.5:
        _place(rec, synth_typing_click(SR), int(pos * SR))
        pos += 0.15
    return rec


def _run(detector, samples):
    bs = detector.config.blocksize
    onsets = 0
    for i in range(samples.shape[0] // bs):
        now = (i + 1) * bs / SR
        if detector.process_block(samples[i * bs:(i + 1) * bs], now) is not None:
            onsets += 1
    return onsets


def test_analyze_recording_shapes_and_trailing_ema():
    rec = synth_silence(BS * 5, seed=2)
    feat = analyze_recording(rec, SR, BS)
    assert feat["peaks"].shape == (5,)
    assert feat["attack_ratio"].shape == (5,)
    # trailing_ema[0] is the pre-recording state (0.0); it lags the energies
    assert feat["trailing_ema"][0] == 0.0


def test_compute_thresholds_are_ordered_and_sane():
    th = compute_thresholds(_tap_recording(), synth_silence(int(10.0 * SR), seed=3),
                            _typing_recording())
    assert th["attack_ratio"] >= 2.0
    assert 0.0 < th["amplitude_floor"] < 0.6
    assert th["centroid_max"] > 0.0


def test_computed_thresholds_accept_taps_reject_typing_and_silence():
    silence = synth_silence(int(10.0 * SR), seed=3)
    th = compute_thresholds(_tap_recording(), silence, _typing_recording())
    cfg = DetectorConfig.from_calibration({"samplerate": SR, "thresholds": th})

    # taps accepted
    tap_track = synth_silence(int(1.0 * SR), seed=20)
    _place(tap_track, synth_knock(SR, seed=200), int(0.3 * SR))
    assert _run(KnockDetector(cfg), tap_track) >= 1

    # typing rejected (high centroid)
    typing_track = synth_silence(int(2.0 * SR), seed=21)
    pos = 0.3
    while pos < 1.7:
        _place(typing_track, synth_typing_click(SR), int(pos * SR))
        pos += 0.13
    assert _run(KnockDetector(cfg), typing_track) == 0

    # silence rejected
    assert _run(KnockDetector(cfg), synth_silence(int(2.0 * SR), seed=22)) == 0


def test_amplitude_floor_between_ambient_and_taps():
    silence = synth_silence(int(10.0 * SR), seed=3)
    taps = _tap_recording()
    th = compute_thresholds(taps, silence, _typing_recording())
    ambient = float(np.percentile(analyze_recording(silence, SR, BS)["peaks"], 99))
    tap_feat = analyze_recording(taps, SR, BS)
    weakest_tap = float(np.min(tap_feat["peaks"][tap_feat["peaks"] > ambient * 4.0]))
    assert ambient < th["amplitude_floor"] < weakest_tap


def test_no_taps_raises():
    import pytest
    silence = synth_silence(int(10.0 * SR), seed=3)
    with pytest.raises(ValueError):
        compute_thresholds(silence, silence, _typing_recording())
