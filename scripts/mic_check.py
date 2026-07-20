"""Mic doctor: is the microphone actually delivering audio to Miko?

Opens the SAME device Miko uses (calibration.json's device_index, else the
system default) at 16kHz and prints a live level meter for ~6 seconds. Knock
on the chassis and say a few words while it runs.

    .venv\\Scripts\\python.exe scripts\\mic_check.py

Verdict:
  - peak stays near 0.000  -> the mic is SILENT. Miko can't hear anything and
    no amount of tuning will help. Fix it in Windows (see the printed hints):
    unmute the mic, disable "audio enhancements" for it, allow desktop apps
    mic access, and close any app (Teams/Zoom/browser) holding the mic.
  - peak jumps when you knock/talk -> the mic is fine; the problem is elsewhere.
"""
import json
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_RATE = 16000
BLOCK = 512
SECONDS = 6.0


def _device():
    cal = REPO_ROOT / "calibration.json"
    if cal.exists():
        try:
            return int(json.loads(cal.read_text())["device_index"])
        except Exception:
            pass
    return None  # system default input


def main():
    dev = _device()
    info = sd.query_devices(dev if dev is not None else sd.default.device[0], "input")
    print(f"device: [{dev if dev is not None else 'default'}] {info['name']}")
    print(f"opening at {SAMPLE_RATE} Hz for {SECONDS:.0f}s — KNOCK and TALK now...\n")

    peak = 0.0
    with sd.InputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK, device=dev,
                        channels=1, dtype="float32") as stream:
        deadline = time.time() + SECONDS
        while time.time() < deadline:
            data, _ = stream.read(BLOCK)
            block = data[:, 0]
            rms = float(np.sqrt(np.mean(block * block)))
            p = float(np.max(np.abs(block)))
            peak = max(peak, p)
            bars = int(min(rms * 400, 40))
            print(f"\rrms {rms:7.4f} peak {p:6.3f} |{'#' * bars:<40}|", end="")
    print("\n")

    print(f"MAX PEAK over the whole run: {peak:.4f}")
    if peak < 0.01:
        print("VERDICT: mic is SILENT. This is a Windows capture problem, not Miko.")
        print("  1. Sound settings -> Input -> pick 'Microphone Array', raise volume, unmute.")
        print("  2. Device properties -> disable ALL 'audio enhancements' for it.")
        print("  3. Privacy & security -> Microphone -> let desktop apps access it.")
        print("  4. Close Teams/Zoom/any browser tab that may hold the mic, then retry.")
        print("  5. Check the physical/Fn mic-mute key.")
    else:
        print("VERDICT: mic is delivering audio. If Miko still doesn't wake, rerun")
        print("  'python -m miko' and knock — the session log will show onsets now.")


if __name__ == "__main__":
    main()
