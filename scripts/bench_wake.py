"""Measure the delegatable segment of the wake path: audio in -> action out.

    python -m scripts.bench_wake            # 200 iterations
    python -m scripts.bench_wake -n 500

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

Excluded, and not measurable here: microphone capture latency (driver + device
buffer), the ring buffer's own fill delay, and the execution time of the real
action (screenshot capture, media key, app launch).

Fixtures: synthetic. The knock generator is `scripts.replay_harness.synth_knock`
-- the same one the replay harness and its tests use -- so this needs no
recorded WAV, no microphone, and no network. If `fixtures/taps.wav` exists it is
still not used: a benchmark needs one isolated, repeatable double-tap, not the
20-pair track.
"""
import argparse
import asyncio
import statistics
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nok.app import GESTURE_POLL_S, SLEEPING, Nok  # noqa: E402
from nok.detector import DetectorConfig, KnockDetector  # noqa: E402
from nok.ioi import DOUBLE_TAP, TRIPLE_TAP, IOIStateMachine  # noqa: E402
from nok.ring_buffer import SAMPLE_RATE  # noqa: E402
from scripts.replay_harness import _place, synth_knock, synth_silence  # noqa: E402

DEFAULT_ITERS = 200
WITHIN_S = 0.2      # gap between the two taps of the pair
LEAD_S = 0.3        # silence before the pair (lets the detector EMA warm up)
TAIL_S = 1.2        # silence after (> close_out, so the chain finalizes)


def build_double_tap(seed):
    """One isolated double-tap on a quiet noise floor.

    Returns (samples, second_onset_index) where the index is the first sample of
    the second knock -- the point from which segment A is timed.
    """
    total = int((LEAD_S + WITHIN_S + TAIL_S) * SAMPLE_RATE)
    track = synth_silence(total, seed=seed)
    first = int(LEAD_S * SAMPLE_RATE)
    second = int((LEAD_S + WITHIN_S) * SAMPLE_RATE)
    _place(track, synth_knock(SAMPLE_RATE, seed=seed), first)
    _place(track, synth_knock(SAMPLE_RATE, seed=seed + 1), second)
    return track, second


def run_once(samples, second_onset_index):
    """Feed one track through detector + IOI. Returns (compute_s, closeout_s).

    Returns None if no DOUBLE_TAP was produced -- the caller treats that as a
    failed iteration rather than silently averaging it away.
    """
    det = KnockDetector(DetectorConfig(samplerate=SAMPLE_RATE))
    machine = IOIStateMachine()
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
    print(f"    p50   : {statistics.median(values):.4f} {unit}")
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
        samples, idx = build_double_tap(seed=i * 7)
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
    print(f"    p50   : {statistics.median(closeout_s) * 1000:.1f} ms")
    print(f"    max   : {max(closeout_s) * 1000:.1f} ms")
    print(f"    note  : IOIStateMachine.close_out = {IOIStateMachine().close_out * 1000:.0f} ms; "
          f"this is a deliberate wait, not compute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
