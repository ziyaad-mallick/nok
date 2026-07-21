"""MIKO v2 — knock-summoned desktop agent.

    knock knock  (right)  -> screenshot            local, instant
    knock knock  (left)   -> media play/pause      local, instant
    knock knock knock     -> live GPT voice agent  sees your screen

Architecture
------------

v1 ran the entire wake pipeline synchronously inside the detector's own run
loop, which meant MIKO was deaf for the whole time it was working -- knocks
arriving mid-cycle were not merely ignored, they were never classified. v2
splits the two:

    [audio callback]  -> RingBuffer (+ SideBuffer)      never blocks
    [detector thread] -> classify -> Gesture -> Queue   never blocks
    [asyncio loop]    -> drains the queue, owns actions and the agent session

The detector's only output is now a queue push, so a knock during a live
conversation is heard and acted on -- that is what makes knock-to-interrupt
possible at all.

Self-hearing is prevented the way v1 solved it: the detector's playback gate is
held while the agent is speaking, so MIKO cannot wake itself on its own voice.
"""
import argparse
import asyncio
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import bindings
from .bindings import MEDIA_PLAY_PAUSE, SCREENSHOT, WAKE_AGENT
from .ioi import IOIStateMachine
from .logs import log_event
from .side import UNKNOWN

_REPO_ROOT = Path(__file__).resolve().parent.parent

# States shared with the overlay.
SLEEPING = "SLEEPING"
WOKE = "WOKE"
LISTENING = "LISTENING"
THINKING = "THINKING"
SPEAKING = "SPEAKING"
ACTING = "ACTING"
ERROR = "ERROR"

GESTURE_POLL_S = 0.02
SESSION_IDLE_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Gesture:
    pattern: str
    side: str
    t: float


class Miko:
    """Owns the gesture queue, the local actions, and the agent session."""

    def __init__(self, *, detector=None, sides_enabled=False, on_state=None,
                 session_factory=None, actions=None):
        self.detector = detector
        self.sides_enabled = sides_enabled
        self.on_state = on_state or (lambda s, d="": None)
        self.session_factory = session_factory
        self.actions = actions

        self.gestures = queue.Queue()
        self.state = SLEEPING
        self._session = None
        self._session_task = None
        self._stop = asyncio.Event()

    # -- called from the detector thread ---------------------------------

    def on_wake(self, pattern, t=None):
        """Detector-thread callback. Must return immediately -- it runs inside
        the detector's classification loop."""
        self.gestures.put(Gesture(pattern, getattr(pattern, "side", UNKNOWN), t or 0.0))

    # -- state -----------------------------------------------------------

    def set_state(self, state, detail=""):
        self.state = state
        log_event("state", state=state, detail=detail)
        try:
            self.on_state(state, detail)
        except Exception as exc:  # an overlay bug must never break the loop
            log_event("observer_error", error=str(exc))

    def _gate_detector(self, on):
        if self.detector is None:
            return
        try:
            self.detector.set_playback_gate(bool(on))
        except AttributeError:
            self.detector.playback_gate = bool(on)

    # -- the loop --------------------------------------------------------

    async def run(self):
        self.set_state(SLEEPING, "ready")
        while not self._stop.is_set():
            try:
                gesture = self.gestures.get_nowait()
            except queue.Empty:
                await asyncio.sleep(GESTURE_POLL_S)
                continue
            try:
                await self._handle(gesture)
            except Exception as exc:
                log_event("gesture_error", error=repr(exc))
                self.set_state(ERROR, str(exc))
                await asyncio.sleep(0.8)
                self.set_state(SLEEPING, "recovered")

    def stop(self):
        self._stop.set()

    async def _handle(self, g: Gesture):
        # A knock while the agent is live always means "stop", whatever the
        # pattern was. Interrupting must never require aiming.
        if self._session is not None:
            log_event("gesture_during_session", pattern=str(g.pattern))
            await self._close_session()
            return

        binding = bindings.resolve(g.pattern, g.side, sides_enabled=self.sides_enabled)
        log_event("gesture", pattern=str(g.pattern), side=g.side, intent=binding.intent)

        if binding.intent == SCREENSHOT:
            await self._instant(binding, self._screenshot)
        elif binding.intent == MEDIA_PLAY_PAUSE:
            await self._instant(binding, self._play_pause)
        elif binding.intent == WAKE_AGENT:
            await self._open_session()

    # -- instant, local, no network --------------------------------------

    async def _instant(self, binding, fn):
        self.set_state(ACTING, binding.label)
        t0 = time.perf_counter()
        try:
            result = await asyncio.to_thread(fn)
        except Exception as exc:
            log_event("instant_action_failed", intent=binding.intent, error=str(exc))
            self.set_state(ERROR, str(exc))
            await asyncio.sleep(0.8)
            self.set_state(SLEEPING)
            return
        dt = time.perf_counter() - t0
        log_event("instant_action", intent=binding.intent, seconds=round(dt, 4),
                  result=result)
        await asyncio.sleep(0.35)  # let the confirmation flourish be seen
        self.set_state(SLEEPING)

    def _screenshot(self):
        from . import screen
        return str(screen.save_png())

    def _play_pause(self):
        from .actions import VK_MEDIA_PLAY_PAUSE, _tap_vk
        _tap_vk(VK_MEDIA_PLAY_PAUSE)
        return "play/pause"

    # -- the agent -------------------------------------------------------

    async def _open_session(self):
        if self.session_factory is None:
            self.set_state(ERROR, "no agent configured")
            await asyncio.sleep(1.0)
            self.set_state(SLEEPING)
            return

        self.set_state(WOKE, "summoned")
        self._session = self.session_factory(on_state=self._session_state)
        self._session_task = asyncio.create_task(self._drive_session())

    def _session_state(self, state, detail=""):
        # Hold the detector's gate while the agent talks so its own voice
        # cannot be classified as a knock (v1's fix, carried forward).
        self._gate_detector(state == SPEAKING)
        self.set_state(state, detail)

    async def _drive_session(self):
        try:
            stats = await asyncio.wait_for(
                self._session.run(), timeout=SESSION_IDLE_TIMEOUT_S
            )
            log_event("session_complete", turns=stats.turns,
                      first_audio=round(stats.first_audio_latency, 3))
        except asyncio.TimeoutError:
            log_event("session_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event("session_failed", error=repr(exc))
            self.set_state(ERROR, str(exc))
            await asyncio.sleep(1.2)
        finally:
            self._session = None
            self._gate_detector(False)
            self.set_state(SLEEPING, "session closed")

    async def _close_session(self):
        session, task = self._session, self._session_task
        self._session = None
        self._session_task = None
        if session is not None:
            session.request_stop()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._gate_detector(False)
        self.set_state(SLEEPING, "closed by knock")


# ==========================================================================
# Production wiring
# ==========================================================================

def build(args):
    """Wire real hardware. Returns (miko, detector, capture, stop_event)."""
    from .audio import CaptureLoop
    from .detector import DetectorConfig, KnockDetector
    from .ring_buffer import RingBuffer
    from .side import SideBuffer, SideClassifier, SideConfig

    cal_path = _REPO_ROOT / "calibration.json"
    cal = _load_calibration(cal_path)

    det_cfg = (DetectorConfig.from_calibration(cal) if cal else DetectorConfig())
    if args.thump:
        det_cfg.simple_mode = True
        det_cfg.simple_amplitude = float(
            os.environ.get("MIKO_THUMP_AMPLITUDE", det_cfg.simple_amplitude)
        )
    floor = os.environ.get("MIKO_KNOCK_FLOOR")
    if floor:
        det_cfg.amplitude_floor = float(floor)

    side_cfg = SideConfig.from_dict(cal.get("sides", {}) if cal else {})
    if args.no_sides:
        side_cfg.enabled = False

    side_buffer = SideBuffer() if side_cfg.enabled else None
    side_clf = SideClassifier(side_buffer, side_cfg) if side_cfg.enabled else None
    detector = KnockDetector(det_cfg, side_classifier=side_clf)

    ring = RingBuffer(seconds=10.0)
    device = args.device if args.device is not None else _resolve_device(cal)
    capture = CaptureLoop(
        ring, device=device,
        channels=(max(side_cfg.left_ch, side_cfg.right_ch) + 1) if side_cfg.enabled else 1,
        side_buffer=side_buffer,
    )

    def session_factory(on_state):
        from .realtime import RealtimeSession
        return RealtimeSession(device=device, on_state=on_state,
                               attach_screen=not args.no_screen)

    miko = Miko(
        detector=detector,
        sides_enabled=side_cfg.enabled,
        session_factory=None if args.no_agent else session_factory,
    )
    return miko, detector, capture, ring, side_cfg


def _load_calibration(path):
    import json
    if not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text())
    except Exception as exc:
        log_event("calibration_load_error", error=str(exc))
        return {}


def _resolve_device(cal):
    try:
        return int(cal["device_index"])
    except (KeyError, TypeError, ValueError):
        return None


def main(argv=None):
    p = argparse.ArgumentParser(prog="miko", description="Knock-summoned desktop agent")
    p.add_argument("--calibrate", action="store_true", help="run the knock calibration wizard")
    p.add_argument("--calibrate-sides", action="store_true", help="calibrate left/right knocks")
    p.add_argument("--thump", action="store_true", help="SIMPLE mode: any loud thump wakes")
    p.add_argument("--device", type=int, default=None, help="input device index")
    p.add_argument("--no-sides", action="store_true", help="ignore side detection")
    p.add_argument("--no-screen", action="store_true", help="don't send screenshots")
    p.add_argument("--no-agent", action="store_true", help="instant actions only, no network")
    p.add_argument("--no-overlay", action="store_true", help="run without the overlay")
    args = p.parse_args(argv)

    if args.calibrate:
        from . import calibrate
        return calibrate.main()
    if args.calibrate_sides:
        from . import calibrate_sides
        return calibrate_sides.main()

    if not args.no_agent and not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set — the agent will fail to connect.")
        print("Get a key at platform.openai.com/api-keys, then:")
        print('    setx OPENAI_API_KEY "sk-..."')
        print("Continuing with instant actions only (--no-agent).\n")
        args.no_agent = True

    miko, detector, capture, ring, side_cfg = build(args)

    overlay = None
    if not args.no_overlay:
        from .overlay import Overlay
        overlay = Overlay()
        miko.on_state = overlay.set_state

    stop_event = threading.Event()
    ioi = IOIStateMachine(
        max_ioi=float(os.environ.get("MIKO_MAX_IOI", "0.6")),
        close_out=float(os.environ.get("MIKO_CLOSE_OUT", "0.75")),
    )

    capture.start()
    threading.Thread(
        target=detector.run, args=(ring, ioi, stop_event),
        kwargs={"on_wake": miko.on_wake}, daemon=True,
    ).start()

    print("MIKO is listening.\n")
    print(bindings.describe(side_cfg.enabled))
    print("\nCtrl-C to quit.")

    loop_thread = threading.Thread(target=lambda: asyncio.run(miko.run()), daemon=True)
    loop_thread.start()

    try:
        if overlay is not None:
            overlay.run()
        else:
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        miko.stop()
        stop_event.set()
        capture.stop()
        if overlay is not None:
            overlay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
