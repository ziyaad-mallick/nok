"""Probe: can this machine hear left-vs-right knocks?

Stage 1 (passive, no user): does any input device expose >1 channel?
Stage 2 (interactive): record N knocks per side, report inter-channel
amplitude + arrival-time deltas. If the deltas don't separate the sides,
left/right dispatch is unbuildable here.
"""

import sys
import time

import numpy as np
import sounddevice as sd

SR = 48000


def stage1():
    print("=== STAGE 1: device channel capability ===")
    multich = []
    for i, d in enumerate(sd.query_devices()):
        n = d["max_input_channels"]
        if n < 1:
            continue
        api = sd.query_hostapis(d["hostapi"])["name"]
        print(f"  [{i:2d}] ch={n}  {api:12s}  {d['name']}")
        if n >= 2:
            multich.append((i, n, d["name"], api))

    print()
    if not multich:
        print("VERDICT: no input device exposes >=2 channels.")
        print("         Left/right knock discrimination is DEAD on this machine.")
        return None
    print(f"VERDICT: {len(multich)} candidate multi-channel device(s).")
    print("         Channels existing != channels carrying spatial info.")
    print("         Stage 2 decides.")
    return multich


def _rec(dev, ch, secs):
    a = sd.rec(int(secs * SR), samplerate=SR, channels=ch, device=dev, dtype="float32")
    sd.wait()
    return a


def _onsets(block):
    """Per-channel peak index + peak amplitude of the loudest transient."""
    env = np.abs(block)
    peak_ch = []
    for c in range(block.shape[1]):
        idx = int(np.argmax(env[:, c]))
        peak_ch.append((idx, float(env[idx, c])))
    return peak_ch


def stage2(dev, ch, name, trials=6):
    print(f"\n=== STAGE 2: spatial separation on [{dev}] {name} ({ch}ch) ===")
    print("You'll knock twice on each side. 2s window per trial.\n")

    results = {}
    for side in ("LEFT", "RIGHT"):
        rows = []
        for t in range(trials):
            input(f"  {side} trial {t+1}/{trials} — press Enter, then knock once: ")
            blk = _rec(dev, ch, 2.0)
            pk = _onsets(blk)
            amps = [p[1] for p in pk]
            idxs = [p[0] for p in pk]
            if max(amps) < 0.01:
                print("    (too quiet, skipped)")
                continue
            # amplitude ratio ch0/ch1 in dB, and arrival delta in samples
            db = 20 * np.log10((amps[0] + 1e-9) / (amps[1] + 1e-9))
            dt = idxs[0] - idxs[1]
            rows.append((db, dt))
            print(f"    amp_delta={db:+6.2f} dB   arrival_delta={dt:+5d} samples")
        results[side] = rows

    print("\n--- RESULT ---")
    for side, rows in results.items():
        if not rows:
            print(f"  {side}: no usable trials")
            continue
        dbs = np.array([r[0] for r in rows])
        dts = np.array([r[1] for r in rows])
        print(f"  {side}: amp {dbs.mean():+.2f} +/- {dbs.std():.2f} dB | "
              f"arrival {dts.mean():+.1f} +/- {dts.std():.1f} samp")

    L, R = results.get("LEFT", []), results.get("RIGHT", [])
    if not L or not R:
        print("\nVERDICT: insufficient data.")
        return
    Ldb = np.array([r[0] for r in L])
    Rdb = np.array([r[0] for r in R])
    sep = abs(Ldb.mean() - Rdb.mean())
    noise = (Ldb.std() + Rdb.std()) / 2 + 1e-9
    print(f"\n  separation = {sep:.2f} dB, within-side spread = {noise:.2f} dB")
    print(f"  d' = {sep / noise:.2f}")
    if sep / noise > 2.0:
        print("\nVERDICT: SEPARABLE. Left/right knock dispatch is buildable.")
    else:
        print("\nVERDICT: NOT SEPARABLE. The array is beamformed/downmixed.")
        print("         Drop left/right from the spec.")


if __name__ == "__main__":
    cands = stage1()
    if not cands:
        sys.exit(1)
    if "--stage2" in sys.argv:
        dev, ch, name, _ = cands[0]
        for c in cands:
            if "Array" in c[2] or "Microphone" in c[2]:
                dev, ch, name = c[0], c[1], c[2]
                break
        stage2(dev, min(ch, 2), name)
    else:
        print("\nRun with --stage2 to test actual spatial separation.")
