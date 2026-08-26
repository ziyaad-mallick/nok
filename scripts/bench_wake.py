"""Measure the delegatable segment of the wake path: audio in -> action out.

    python scripts/bench_wake.py            # 200 iterations
    python scripts/bench_wake.py -n 500

The script-path form above works from any directory (the `sys.path` insert below
resolves the repo root from `__file__`). `python -m scripts.bench_wake` also
works, but only from the repo root -- there is no `scripts/__init__.py`, so `-m`
depends on the cwd being on `sys.path`. Prefer the script-path form.

numpy is the only third-party package this needs. The full `requirements.txt`
installs the app's audio, vision and agent stacks, none of which are imported on
the measured path.

What this measures, exactly
---------------------------

The real end-to-end path is:

    physical knock -> microphone -> capture -> detector -> IOI -> dispatch -> action

Only the middle of that can be measured without a person knocking on a laptop.
This harness measures the middle, in three separately-reported parts, and makes
no claim about the two ends:

  A. detect->decide compute
     Wall-clock CPU time to push every 512-sample block from the second knock's
     onset through `KnockDetector.process_block` + `IOIStateMachine`, up to and
     including the block on which DOUBLE_TAP is emitted. Blocks are fed as fast
     as the CPU allows, so this contains no real-time waiting: it is the compute
     the detector thread must do, not the time a user waits.

  B. decide->dispatch handoff
     Wall-clock from `Nok.on_wake()` (the detector-thread callback) to a stubbed
     action function being entered, through the real `Nok` asyncio loop, the
     real gesture `queue.Queue`, and the real `bindings.resolve`. The action
     itself is replaced by a no-op, so nothing here is the action's own cost.
     This is bounded below by `nok.app.GESTURE_POLL_S`, the loop's poll period.

  C. close-out window (a property of the design, not of this machine)
     Signal-time distance from the second onset to the DOUBLE_TAP decision. The
     IOI state machine deliberately waits `close_out` seconds of silence after
     the last tap so a double can still grow into a triple. No amount of CPU
     removes it, and it dominates both A and B by orders of magnitude. Reported
     in audio seconds, derived from sample counts, so it is exact.

     Measured against the tuning `nok.app.ioi_settings()` resolves -- including
     the NOK_CLOSE_OUT / NOK_MAX_IOI overrides -- not `IOIStateMachine`'s looser
     library defaults, so that C is the wait a real user of *this* install
     experiences. The reported figure is `close_out` plus 0-32ms, because the
     decision cannot land before the 512-sample block in flight completes; the
     onset phase is swept across the block grid so that spread is visible
     instead of collapsed to one point.

A is deterministic in what it computes -- fixtures are seeded -- but A and B are
both wall-clock, so their percentiles move with CPU load. B in particular is
bounded below by the loop's 20 ms poll period and is dominated by the OS
scheduler; treat it as "0-20 ms, bounded by GESTURE_POLL_S" rather than as a
constant. Only C is machine-independent. Run on an otherwise idle machine.

Excluded, and not measurable here: microphone capture latency (driver + device
buffer), the ring buffer's own fill delay, and the execution time of the real
action (screenshot capture, media key, app launch).

Not free of side effects: each dispatch drives `Nok.set_state` -> `log_event`,
which enqueues a record onto a `QueueHandler`. The write and flush happen on the
`QueueListener` thread, so the timed window for B pays a queue put and not the
disk. It does leave JSONL files in `session-logs/` (gitignored) behind.

Fixtures: synthetic. The knock generator is `scripts.replay_harness.synth_knock`
-- the same one the replay harness and its tests use -- so this needs no
recorded WAV, no microphone, and no network. If `fixtures/taps.wav` exists it is
still not used: a benchmark needs one isolated, repeatable double-tap, not the
20-pair track.
"""
import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nok.app import GESTURE_POLL_S, SLEEPING, Nok, ioi_settings  # noqa: E402
from nok.detector import DetectorConfig, KnockDetector  # noqa: E402
from nok.ioi import DOUBLE_TAP, TRIPLE_TAP, IOIStateMachine  # noqa: E402
from nok.ring_buffer import SAMPLE_RATE  # noqa: E402
from scripts.replay_harness import _place, synth_knock, synth_silence  # noqa: E402

DEFAULT_ITERS = 200
WITHIN_S = 0.2      # gap between the two taps of the pair
LEAD_S = 0.3        # silence before the pair (lets the detector EMA warm up)

# Segment C is measured against the tuning the *application* resolves --
# `nok.app.ioi_settings()`, which reads NOK_MAX_IOI / NOK_CLOSE_OUT -- and not
# against `IOIStateMachine()`'s looser library defaults (MAX_IOI 0.45,
# CLOSE_OUT 0.60). Benchmarking the library defaults would report a window no
# user of Nok ever waits; hardcoding a copy of the app's defaults here would
# report the wrong window for anyone who has set the environment variable.
IOI_SETTINGS = ioi_settings()

# Silence after the pair. Must exceed close_out or the chain never finalizes
# inside the fixture and every iteration fails, so it is derived rather than
# fixed -- NOK_CLOSE_OUT can be set to anything.
TAIL_S = IOI_SETTINGS["close_out"] + 0.45


def build_double_tap(seed, phase_samples=0):
    """One isolated double-tap on a quiet noise floor.

    Returns (samples, second_onset_index) where the index is the first sample of
    the second knock -- the point from which segment A is timed.

    `phase_samples` shifts the pair within the block grid. A real knock lands at
    an arbitrary offset inside the 512-sample block the detector happens to be
    filling, and the decision cannot arrive until that block completes, so the
    close-out window a user waits is `close_out` plus 0-32ms of quantization.
    Pinning the phase at zero would report a single point from that range 200
    times over and print it as a distribution -- p50 and p95 identical, which
    looks like precision and is actually one measurement repeated.
    """
    total = int((LEAD_S + WITHIN_S + TAIL_S) * SAMPLE_RATE)
    track = synth_silence(total, seed=seed)
    first = int(LEAD_S * SAMPLE_RATE) + phase_samples
    second = int((LEAD_S + WITHIN_S) * SAMPLE_RATE) + phase_samples
    _place(track, synth_knock(SAMPLE_RATE, seed=seed), first)
    _place(track, synth_knock(SAMPLE_RATE, seed=seed + 1), second)
    return track, second


def run_once(samples, second_onset_index):
    """Feed one track through detector + IOI. Returns (compute_s, closeout_s).

    Returns None if no DOUBLE_TAP was produced -- the caller treats that as a
    failed iteration rather than silently averaging it away.
    """
    det = KnockDetector(DetectorConfig(samplerate=SAMPLE_RATE))
    machine = IOIStateMachine(**IOI_SETTINGS)
    bs = det.config.blocksize
    n_blocks = samples.shape[0] // bs
    start_block = second_onset_index // bs

    compute = 0.0
    for i in range(n_blocks):
        block = samples[i * bs:(i + 1) * bs]
        now = (i + 1) * bs / SAMPLE_RATE
        timed = i >= start_block

        if timed:
            t0 = time.perf_counter()
        onset = det.process_block(block, now)
        decision = machine.feed(onset) if onset is not None else None
        if decision not in (DOUBLE_TAP, TRIPLE_TAP):
            decision = machine.poll(now)
        if timed:
            compute += time.perf_counter() - t0

        if decision in (DOUBLE_TAP, TRIPLE_TAP):
            closeout = now - (second_onset_index / SAMPLE_RATE)
            return compute, closeout
    return None


class DispatchRig:
    """A real `Nok` on a real asyncio loop, with every action stubbed out."""

    def __init__(self):
        self.fired = threading.Event()
        self.t_fired = 0.0
        self.nok = Nok(sides_enabled=False, session_factory=None, fav_app="")
        # Replace the three instant actions. `_handle` -> `_instant` -> these,
        # via asyncio.to_thread, is otherwise the untouched production path.
        self.nok._screenshot = self._stub
        self.nok._play_pause = self._stub
        self.nok._open_fav = self._stub
        # Notifications are a UI concern with a Win32 cost; the default on_notify
        # is already a no-op lambda, so nothing to stub there.
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self.nok.run()), daemon=True
        )

    def _stub(self):
        self.t_fired = time.perf_counter()
        self.fired.set()
        return "stub"

    def start(self):
        self._thread.start()
        time.sleep(0.2)  # let the loop reach its first poll

    def stop(self):
        self.nok.stop()
        self._thread.join(timeout=2.0)

    def wait_idle(self, timeout=5.0):
        """Block until the loop is back in SLEEPING and polling.

        Without this the measurement is wrong in a specific and flattering-to-
        nobody way: `Nok._instant` ends with `await asyncio.sleep(0.35)` (the
        confirmation flourish), so a gesture queued immediately after a previous
        one waits out that sleep and reads ~350ms. A user knocking at an idle
        Nok does not pay it. See the report for the back-to-back number.
        """
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.nok.state == SLEEPING and self.nok.gestures.empty():
                # One extra poll period so the loop is parked in its sleep, not
                # mid-transition out of the previous action.
                time.sleep(GESTURE_POLL_S * 2)
                return True
            time.sleep(0.005)
        return False

    def dispatch_once(self, timeout=5.0):
        """Time on_wake -> stubbed action entered. Returns seconds, or None."""
        self.fired.clear()
        t0 = time.perf_counter()
        self.nok.on_wake(DOUBLE_TAP, t0)
        if not self.fired.wait(timeout):
            return None
        return self.t_fired - t0


def pct(values, p):
    """Nearest-rank percentile. Avoids interpolating between real samples."""
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(p / 100.0 * len(ordered) + 0.5)) - 1))
    return ordered[k]


def summarize(name, values, unit="ms"):
    print(f"{name}")
    print(f"    n     : {len(values)}")
    print(f"    min   : {min(values):.4f} {unit}")
    print(f"    p50   : {pct(values, 50):.4f} {unit}")
    print(f"    p95   : {pct(values, 95):.4f} {unit}")
    print(f"    max   : {max(values):.4f} {unit}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-n", "--iterations", type=int, default=DEFAULT_ITERS)
    args = ap.parse_args()
    n = args.iterations

    print("=== Nok wake-path benchmark ===")
    print(f"python     : {sys.version.split()[0]}")
    print(f"samplerate : {SAMPLE_RATE} Hz, blocksize 512 "
          f"({512 / SAMPLE_RATE * 1000:.1f} ms of audio per block)")
    print(f"fixtures   : synthetic (scripts.replay_harness.synth_knock)")
    print(f"iterations : {n}")
    print()

    # --- A: detect -> decide -------------------------------------------------
    compute_ms, closeout_s, failures = [], [], 0
    for i in range(n):
        # Sweep the onset across one full block so C reports the real
        # quantization spread rather than one alignment repeated n times.
        phase = (i * 37) % DetectorConfig(samplerate=SAMPLE_RATE).blocksize
        samples, idx = build_double_tap(seed=i * 7, phase_samples=phase)
        result = run_once(samples, idx)
        if result is None:
            failures += 1
            continue
        compute_ms.append(result[0] * 1000.0)
        closeout_s.append(result[1])

    if not compute_ms:
        print("FAIL: no double-tap was detected in any iteration.")
        return 1

    summarize("A. detect->decide compute (audio blocks -> DOUBLE_TAP)", compute_ms)
    print(f"    missed: {failures}/{n} iterations produced no DOUBLE_TAP")
    print()

    # --- B: decide -> dispatch ----------------------------------------------
    rig = DispatchRig()
    rig.start()
    try:
        dispatch_ms, stalled = [], 0
        for _ in range(n):
            if not rig.wait_idle():
                stalled += 1
                continue
            dt = rig.dispatch_once()
            if dt is not None:
                dispatch_ms.append(dt * 1000.0)
        # Same handoff, but with a second gesture queued while the previous
        # action's confirmation flourish is still running. Reported separately
        # because it is a real user-visible case, not the headline number.
        rig.wait_idle()
        rig.dispatch_once()
        back_to_back = rig.dispatch_once()
    finally:
        rig.stop()

    if not dispatch_ms:
        print("FAIL: the stubbed action never fired.")
        return 1

    summarize("B. decide->dispatch handoff (on_wake -> stubbed action entered, "
              "loop idle)", dispatch_ms)
    print(f"    note  : the loop polls every GESTURE_POLL_S = "
          f"{GESTURE_POLL_S * 1000:.0f} ms, so a gesture waits 0-{GESTURE_POLL_S * 1000:.0f} ms "
          f"for the next poll depending on where in the period it lands")
    print(f"    stalled: {stalled}/{n} iterations never returned to SLEEPING")
    if back_to_back is not None:
        print(f"    back-to-back (gesture queued during the previous action's "
              f"0.35s flourish): {back_to_back * 1000:.1f} ms")
    print()

    # --- A + B ---------------------------------------------------------------
    combined = [a + b for a, b in zip(compute_ms, dispatch_ms)]
    summarize("A+B. detection to dispatch", combined)
    print("    method  : element-wise sum of independently sampled A and B, so "
          "this is an estimate of the combined distribution, not paired runs")
    print("    excludes: microphone capture latency, and the action's own execution")
    print()

    # --- C: the design's own wait -------------------------------------------
    print("C. close-out window (signal time, second onset -> DOUBLE_TAP)")
    print(f"    min   : {min(closeout_s) * 1000:.1f} ms")
    print(f"    p50   : {pct(closeout_s, 50) * 1000:.1f} ms")
    print(f"    p95   : {pct(closeout_s, 95) * 1000:.1f} ms")
    print(f"    max   : {max(closeout_s) * 1000:.1f} ms")
    print(f"    note  : close_out = {IOI_SETTINGS['close_out'] * 1000:.0f} ms as resolved by "
          f"nok.app.ioi_settings() (set NOK_CLOSE_OUT to change it); the spread above "
          f"is block quantization. A deliberate wait, not compute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
