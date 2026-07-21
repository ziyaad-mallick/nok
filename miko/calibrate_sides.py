"""Left/right knock calibration.

Records labelled knocks from each side of the chassis, fits a decision boundary,
and — crucially — refuses to enable side detection if the two sides are not
actually separable.

Laptop mic arrays run DSP beamforming before the audio reaches Windows. On some
machines that preserves enough inter-channel level difference to tell left from
right; on others it flattens it entirely. Rather than assume, this measures d'
(class separation over within-class spread) and only writes `enabled: true` when
the margin is real. A coin flip bound to a media key is worse than no binding.

Result is merged into calibration.json under "sides".
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

from .side import MIN_SEPARATION_DPRIME, calibrate_sides

_REPO_ROOT = Path(__file__).resolve().parent.parent
CAL_PATH = _REPO_ROOT / "calibration.json"

TRIALS_PER_SIDE = 8
WINDOW_S = 1.5
MIN_PEAK = 0.02  # below this the knock was too soft to measure


def _pick_device():
    """Highest-channel-count input device — the mic array, not a mono headset."""
    import sounddevice as sd

    best, best_ch = None, 0
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_input_channels"]
        if ch > best_ch:
            best, best_ch = i, ch
    return best, best_ch


def _record_knock(device, channels, samplerate=48000):
    """Capture one window; return per-channel peak amplitudes."""
    import sounddevice as sd

    rec = sd.rec(int(WINDOW_S * samplerate), samplerate=samplerate,
                 channels=channels, device=device, dtype="float32")
    sd.wait()
    return np.max(np.abs(rec), axis=0)


def _collect(device, channels, side, trials):
    samples = []
    for i in range(trials):
        input(f"  {side} {i + 1}/{trials} — press Enter, then knock once: ")
        peaks = _record_knock(device, channels)
        if float(np.max(peaks)) < MIN_PEAK:
            print("    too quiet — knock harder; retrying")
            continue
        samples.append(peaks)
        print(f"    peaks: {np.round(peaks, 4)}")
        time.sleep(0.15)
    return samples


def main(argv=None):
    device, channels = _pick_device()
    if device is None or channels < 2:
        print("No multi-channel input device found.")
        print("Side detection is impossible on this machine — MIKO will use the")
        print("side-agnostic gesture map (double = screenshot, triple = agent).")
        _write({"enabled": False, "reason": "no multi-channel device"})
        return 1

    channels = min(channels, 4)
    print(f"Using device {device} with {channels} channels.\n")
    print("Knock on the LEFT side of the chassis, then the RIGHT.")
    print("Knock where you naturally would — same spot, same force, each time.\n")

    left = _collect(device, channels, "LEFT", TRIALS_PER_SIDE)
    print()
    right = _collect(device, channels, "RIGHT", TRIALS_PER_SIDE)

    if not left or not right:
        print("\nNot enough usable knocks. Side detection stays off.")
        _write({"enabled": False, "reason": "insufficient samples"})
        return 1

    # Try every channel pair; keep the one that separates the sides best.
    best, best_d = None, -1.0
    for a in range(channels):
        for b in range(channels):
            if a == b:
                continue
            got = calibrate_sides(left, right, left_ch=a, right_ch=b)
            if got["d_prime"] > best_d:
                best, best_d = got, got["d_prime"]

    print("\n" + "=" * 58)
    print(f"  best channel pair : {best['left_ch']} vs {best['right_ch']}")
    print(f"  separation        : {best['separation_db']} dB")
    print(f"  within-side spread: {best['pooled_sd_db']} dB")
    print(f"  d'                : {best['d_prime']}  (need >= {MIN_SEPARATION_DPRIME})")
    print("=" * 58)

    if best["enabled"]:
        print("\n  SEPARABLE — left/right knocks enabled.\n")
        print("    knock knock (right) -> screenshot")
        print("    knock knock (left)  -> play / pause")
        print("    knock knock knock   -> wake the agent")
    else:
        print(f"\n  NOT SEPARABLE — {best['reason']}")
        print("  This mic array beamforms away the spatial difference.")
        print("  Falling back to the side-agnostic map:\n")
        print("    knock knock         -> screenshot")
        print("    knock knock knock   -> wake the agent")

    _write(best, device=device)
    print(f"\nWritten to {CAL_PATH}")
    return 0


def _write(result, device=None):
    cal = {}
    if CAL_PATH.exists():
        try:
            cal = json.loads(CAL_PATH.read_text())
        except Exception:
            cal = {}
    cal["sides"] = result
    if device is not None:
        cal.setdefault("device_index", device)
    CAL_PATH.write_text(json.dumps(cal, indent=2))


if __name__ == "__main__":
    sys.exit(main())
