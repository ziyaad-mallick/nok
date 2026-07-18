"""Per-laptop knock calibration wizard.

    python -m miko.calibrate

Steps:
  1. list input devices, pick one, pin its index;
  2. Windows checklist -- mic "audio enhancements"/AGC must be OFF (they apply
     dynamic gain that destroys the sharp-attack signature we key on);
  3. record "tap 10 times", 10s silence, 10s typing; compute classifier
     thresholds (attack ratio, centroid band, amplitude floor) with margin;
  4. save calibration.json (device index + thresholds + samplerate).

The threshold math is factored into pure functions (analyze_recording /
compute_thresholds) so it is unit-tested without any audio hardware. It is
demo-critical -- kept explicit and commented, not clever.
"""
import json
import sys
from pathlib import Path

import numpy as np

from .detector import spectral_centroid
from .ring_buffer import SAMPLE_RATE

BLOCKSIZE = 512
EMA_ALPHA = 0.15  # must match DetectorConfig.ema_alpha so trailing avgs agree

# Margin factors (explicit, so the calibration is auditable).
ONSET_FACTOR = 4.0     # a block is a real "event" if peak > ONSET_FACTOR * ambient
SILENCE_MARGIN = 4.0   # amplitude floor sits this far above ambient noise...
TAP_FLOOR_FRAC = 0.6   # ...but never above this fraction of the weakest tap
ATTACK_MARGIN = 0.6    # keep attack threshold below the weakest tap's attack
MIN_ATTACK = 2.0       # hard minimum so noise can't sneak under a tiny threshold
CENTROID_MARGIN = 1.3  # if typing overlaps taps, widen the tap band by this

CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "calibration.json"


# --------------------------------------------------------------------------
# Pure analysis (unit-tested)
# --------------------------------------------------------------------------

def analyze_recording(samples, sr=SAMPLE_RATE, blocksize=BLOCKSIZE, ema_alpha=EMA_ALPHA):
    """Per-block features over a recording, matching the live detector exactly.

    Returns arrays: peaks, energies, trailing_ema (avg of PRIOR blocks),
    attack_ratio (energy / trailing_ema), centroids. One entry per whole block.
    """
    samples = np.asarray(samples, dtype=np.float32)
    n_blocks = samples.shape[0] // blocksize
    peaks = np.zeros(n_blocks, dtype=np.float64)
    energies = np.zeros(n_blocks, dtype=np.float64)
    trailing = np.zeros(n_blocks, dtype=np.float64)
    attack = np.zeros(n_blocks, dtype=np.float64)
    centroids = np.zeros(n_blocks, dtype=np.float64)

    ema = 0.0
    for i in range(n_blocks):
        b = samples[i * blocksize:(i + 1) * blocksize]
        pk = float(np.max(np.abs(b)))
        en = float(np.dot(b, b) / b.size)
        peaks[i] = pk
        energies[i] = en
        trailing[i] = ema                      # trailing avg BEFORE this block
        attack[i] = en / (ema + 1e-9)
        centroids[i] = spectral_centroid(b, sr)
        ema = (1.0 - ema_alpha) * ema + ema_alpha * en

    return {
        "peaks": peaks,
        "energies": energies,
        "trailing_ema": trailing,
        "attack_ratio": attack,
        "centroids": centroids,
    }


def _onset_mask(feat, ambient):
    """Blocks that stand clearly above the ambient noise floor."""
    return feat["peaks"] > ambient * ONSET_FACTOR


def compute_thresholds(tap_samples, silence_samples, typing_samples,
                       sr=SAMPLE_RATE, blocksize=BLOCKSIZE, ema_alpha=EMA_ALPHA):
    """Derive {attack_ratio, centroid_max, amplitude_floor} from three
    recordings. Explicit and commented -- this is the demo's crown jewel."""
    tap = analyze_recording(tap_samples, sr, blocksize, ema_alpha)
    sil = analyze_recording(silence_samples, sr, blocksize, ema_alpha)
    typ = analyze_recording(typing_samples, sr, blocksize, ema_alpha)

    # Ambient noise ceiling from the silent recording (99th pct = worst hiss).
    ambient = float(np.percentile(sil["peaks"], 99)) if sil["peaks"].size else 0.0

    tap_mask = _onset_mask(tap, ambient)
    if not tap_mask.any():
        raise ValueError("no taps detected above the noise floor -- re-record")
    tap_peaks = tap["peaks"][tap_mask]
    tap_attacks = tap["attack_ratio"][tap_mask]
    tap_centroids = tap["centroids"][tap_mask]

    # -- amplitude floor: safely above ambient, comfortably below every tap --
    weakest_tap = float(np.min(tap_peaks))
    amplitude_floor = float(min(ambient * SILENCE_MARGIN, weakest_tap * TAP_FLOOR_FRAC))

    # -- attack ratio: below the weakest tap's attack so all taps clear it,
    #    but never below MIN_ATTACK (else steady noise could pass) --
    weakest_tap_attack = float(np.min(tap_attacks))
    attack_ratio = float(max(MIN_ATTACK, weakest_tap_attack * ATTACK_MARGIN))

    # -- centroid band: taps are low-frequency, typing clicks are high. Put the
    #    edge between the two populations if they separate; else widen the tap
    #    band by a margin --
    tap_centroid_hi = float(np.percentile(tap_centroids, 90))
    typ_mask = _onset_mask(typ, ambient)
    if typ_mask.any():
        typing_centroid_lo = float(np.percentile(typ["centroids"][typ_mask], 10))
    else:
        typing_centroid_lo = float("inf")
    if typing_centroid_lo > tap_centroid_hi:
        centroid_max = 0.5 * (tap_centroid_hi + typing_centroid_lo)
        if not np.isfinite(centroid_max):
            centroid_max = tap_centroid_hi * CENTROID_MARGIN
    else:
        centroid_max = tap_centroid_hi * CENTROID_MARGIN

    return {
        "attack_ratio": round(attack_ratio, 4),
        "centroid_max": round(float(centroid_max), 2),
        "amplitude_floor": round(amplitude_floor, 6),
    }


# --------------------------------------------------------------------------
# Interactive wizard (not unit-tested; uses hardware)
# --------------------------------------------------------------------------

def list_input_devices():
    """Return [(index, name, max_input_channels), ...] for input-capable devices."""
    import sounddevice as sd
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append((idx, dev["name"], dev["max_input_channels"]))
    return out


def _prompt_device():
    devices = list_input_devices()
    print("\nInput devices:")
    for idx, name, ch in devices:
        print(f"  [{idx}] {name}  ({ch} ch)")
    valid = {idx for idx, _, _ in devices}
    while True:
        raw = input("\nPick input device index: ").strip()
        try:
            choice = int(raw)
        except ValueError:
            print("  enter a number")
            continue
        if choice in valid:
            return choice
        print("  not an input device index")


def _confirm_enhancements():
    print("\n--- Calibration step 0: DISABLE mic processing (Windows) ---")
    print("  Settings > System > Sound > (your mic) > Device properties >")
    print("  Additional device properties > Advanced / Enhancements tab:")
    print("    * turn OFF 'Audio enhancements'")
    print("    * turn OFF AGC / 'automatic gain control' / noise suppression")
    print("  These apply dynamic gain that flattens the sharp attack knocks rely on.")
    while True:
        ans = input("Confirmed enhancements + AGC are OFF? (yes/no): ").strip().lower()
        if ans in ("y", "yes"):
            return
        print("  Please turn them off before continuing.")


def _record(seconds, device, sr=SAMPLE_RATE):
    import sounddevice as sd
    print(f"  recording {seconds:.0f}s ...")
    frames = int(seconds * sr)
    data = sd.rec(frames, samplerate=sr, channels=1, dtype="float32", device=device)
    sd.wait()
    return data.reshape(-1).astype(np.float32)


def main():
    print("=== Miko knock calibration ===")
    device = _prompt_device()
    _confirm_enhancements()

    input("\nReady to record. Press Enter, then TAP THE CHASSIS 10 TIMES...")
    taps = _record(10.0, device)
    input("Now stay SILENT. Press Enter for 10s of silence...")
    silence = _record(10.0, device)
    input("Now TYPE normally. Press Enter for 10s of typing...")
    typing = _record(10.0, device)

    thresholds = compute_thresholds(taps, silence, typing)
    payload = {
        "device_index": device,
        "samplerate": SAMPLE_RATE,
        "thresholds": thresholds,
    }
    with open(CALIBRATION_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"\nSaved {CALIBRATION_PATH}")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
