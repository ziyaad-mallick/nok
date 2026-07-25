"""Guided recording of the detector test fixtures, using the pinned device.

    python -m scripts.record_fixtures

Reads the pinned input device from calibration.json (run `python -m
nok.calibrate` first) and records three 16kHz mono WAVs into fixtures/:

    taps.wav    20 deliberate double-taps
    typing.wav  2 minutes of typing
    bumps.wav   desk bumps, trackpad clicks, mug set-downs

These feed scripts/replay_harness.py for the Day-1 kill criterion.
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nok.ring_buffer import SAMPLE_RATE  # noqa: E402
from scripts.replay_harness import write_wav  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"
CALIBRATION_PATH = REPO_ROOT / "calibration.json"

# (filename, seconds, prompt)
FIXTURES = [
    ("taps.wav", 60.0,
     "Knock 20 DELIBERATE DOUBLE-TAPS on the chassis, ~1s between pairs."),
    ("typing.wav", 120.0,
     "TYPE normally and continuously for the full 2 minutes."),
    ("bumps.wav", 60.0,
     "Make DESK BUMPS: trackpad clicks, set a mug down, tap the desk (NOT the chassis)."),
]


def _pinned_device():
    if not CALIBRATION_PATH.exists():
        print(f"No {CALIBRATION_PATH.name}. Run `python -m nok.calibrate` first.")
        sys.exit(1)
    with open(CALIBRATION_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return int(data["device_index"]), int(data.get("samplerate", SAMPLE_RATE))


def _record(seconds, device, sr):
    import sounddevice as sd
    print(f"  recording {seconds:.0f}s ...")
    frames = int(seconds * sr)
    data = sd.rec(frames, samplerate=sr, channels=1, dtype="float32", device=device)
    sd.wait()
    return data.reshape(-1).astype(np.float32)


def main():
    device, sr = _pinned_device()
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== Recording fixtures (device {device}, {sr} Hz) ===")

    for name, seconds, prompt in FIXTURES:
        print(f"\n--- {name} ---")
        print(f"  {prompt}")
        input("  Press Enter to start...")
        samples = _record(seconds, device, sr)
        path = FIXTURES_DIR / name
        write_wav(path, samples, sr)
        print(f"  saved {path}")

    print("\nAll fixtures recorded. Run: python -m scripts.replay_harness")
    return 0


if __name__ == "__main__":
    sys.exit(main())
