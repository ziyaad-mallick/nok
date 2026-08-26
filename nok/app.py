"""NOK v2 — knock on your laptop, something happens.

    knock knock  (right)  -> screenshot            local, instant
    knock knock  (left)   -> media play/pause      local, instant
                          -> or open --fav APP     local, instant
    knock knock knock     -> an agent              optional; four backends

By default there is no window: a tray icon, and a balloon per action (notify.py).
The overlay is opt-in via --overlay.

Architecture
------------

v1 ran the entire wake pipeline synchronously inside the detector's own run
loop, which meant NOK was deaf for the whole time it was working -- knocks
arriving mid-cycle were not merely ignored, they were never classified. v2
splits the two:

    [audio callback]  -> RingBuffer (+ SideBuffer)      never blocks
    [detector thread] -> classify -> Gesture -> Queue   never blocks
    [asyncio loop]    -> drains the queue, owns actions and the agent session

The detector's only output is now a queue push, so a knock during a live
conversation is heard and acted on -- that is what makes knock-to-interrupt
possible at all.

Self-hearing is prevented the way v1 solved it: the detector's playback gate is
held while the agent is speaking, so NOK cannot wake itself on its own voice.
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
from .bindings import MEDIA_PLAY_PAUSE, OPEN_FAV_APP, SCREENSHOT, WAKE_AGENT
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

# The IOI tuning this application runs with, which is looser than
# IOIStateMachine's own library defaults. Exposed as a function rather than
# constants so that anything measuring the app -- scripts/bench_wake.py --
# resolves the same environment the app does, instead of hardcoding a copy that
# silently drifts and ignores the user's override.
def ioi_settings() -> dict:
    return {
        "max_ioi": float(os.environ.get("NOK_MAX_IOI", "0.6")),
        "close_out": float(os.environ.get("NOK_CLOSE_OUT", "0.75")),
    }
SESSION_IDLE_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Gesture:
    pattern: str
    side: str
    t: float


class Nok:
    """Owns the gesture queue, the local actions, and the agent session."""

    def __init__(self, *, detector=None, sides_enabled=False, on_state=None,
                 on_level=None, session_factory=None, actions=None,
                 fav_app="", on_notify=None):
        self.detector = detector
        self.sides_enabled = sides_enabled
        self.on_state = on_state or (lambda s, d="": None)
        self.on_level = on_level or (lambda source, rms: None)
        self.session_factory = session_factory
        self.actions = actions
        self.fav_app = fav_app
        # Default UI: a notification per action, no window. See notify.py.
        self.on_notify = on_notify or (lambda title, text="": None)

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

        binding = bindings.resolve(g.pattern, g.side, sides_enabled=self.sides_enabled,
                                   fav_app=self.fav_app)
        log_event("gesture", pattern=str(g.pattern), side=g.side, intent=binding.intent)

        if binding.intent == SCREENSHOT:
            await self._instant(binding, self._screenshot)
        elif binding.intent == MEDIA_PLAY_PAUSE:
            await self._instant(binding, self._play_pause)
        elif binding.intent == OPEN_FAV_APP:
            await self._instant(binding, self._open_fav)
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
        self._notify_action(binding.intent, result)
        await asyncio.sleep(0.35)  # let the confirmation flourish be seen
        self.set_state(SLEEPING)

    def _notify_action(self, intent, result):
        """Tell the user it happened. With no overlay this is the only feedback
        a silent local action produces."""
        from .notify import action_message
        title, body = action_message(intent, str(result))
        if not title:
            return
        try:
            self.on_notify(title, body)
        except Exception as exc:
            log_event("notify_error", error=str(exc))

    def _screenshot(self):
        from . import screen
        return str(screen.save_png())

    def _play_pause(self):
        from .actions import VK_MEDIA_PLAY_PAUSE, _tap_vk
        _tap_vk(VK_MEDIA_PLAY_PAUSE)
        return "play/pause"

    def _open_fav(self):
        from .actions import open_app
        return open_app(self.fav_app)

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

    async def _pump_levels(self):
        """Feed live mic/output amplitude to the overlay at render cadence.

        Polling the session is deliberate: the audio callbacks must never call
        into the UI, and a missed frame costs nothing.
        """
        while self._session is not None:
            try:
                mic, out = self._session.levels()
                self.on_level("mic", mic)
                self.on_level("out", out)
            except Exception:
                pass
            await asyncio.sleep(1 / 30)

    async def _drive_session(self):
        levels = asyncio.create_task(self._pump_levels())
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
            levels.cancel()
            await asyncio.gather(levels, return_exceptions=True)
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
    """Wire real hardware. Returns (nok, detector, capture, stop_event)."""
    from .audio import CaptureLoop
    from .detector import DetectorConfig, KnockDetector
    from .ring_buffer import RingBuffer
    from .side import SideBuffer, SideClassifier, SideConfig

    cal_path = _REPO_ROOT / "calibration.json"
    cal = _load_calibration(cal_path)

    try:
        det_cfg = DetectorConfig.from_calibration(cal) if cal else DetectorConfig()
    except (TypeError, ValueError) as exc:
        # Valid JSON holding nonsense values. Defaults still detect knocks;
        # refusing to start does not.
        log_event("calibration_invalid", error=str(exc))
        det_cfg = DetectorConfig()
    if args.thump:
        det_cfg.simple_mode = True
        det_cfg.simple_amplitude = float(
            os.environ.get("NOK_THUMP_AMPLITUDE", det_cfg.simple_amplitude)
        )
    floor = os.environ.get("NOK_KNOCK_FLOOR")
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

    nok = None  # bound below; the factory closes over it for notifications

    def session_factory(on_state):
        # Default: spoken commands, matched locally and executed. No key, no
        # model server, no network -- and no LLM latency between saying a thing
        # and it happening.
        if args.vision:
            from .vision import VisionSession
            return VisionSession(on_state=on_state, on_answer=nok.on_notify)
        if args.realtime:
            from .realtime import RealtimeSession
            return RealtimeSession(device=device, on_state=on_state,
                                   attach_screen=not args.no_screen)
        if args.chat:
            from .local import LocalSession
            return LocalSession(device=device, on_state=on_state,
                                attach_screen=not args.no_screen)
        from .voice import VoiceCommandSession
        return VoiceCommandSession(device=device, on_state=on_state,
                                   speak=args.speak)

    nok = Nok(
        detector=detector,
        sides_enabled=side_cfg.enabled,
        session_factory=None if args.no_agent else session_factory,
        fav_app=getattr(args, "fav", "") or "",
    )
    return nok, detector, capture, ring, side_cfg


def _load_calibration(path):
    import json
    if not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text())
    except Exception as exc:
        log_event("calibration_load_error", error=str(exc))
        return {}


def _watch_threads(nok, stop_event, threads, poll_s=2.0):
    """Say something out loud when a background worker dies on its own.

    Every worker is a daemon, so one of them dying takes nothing else down --
    the overlay keeps painting and the process looks healthy while being partly
    or wholly unresponsive. Noticing is the entire job here.
    """
    def loop():
        alive = dict.fromkeys(threads, True)
        while not stop_event.wait(poll_s):
            for name, thread in threads.items():
                if alive[name] and not thread.is_alive():
                    alive[name] = False
                    log_event("thread_died", thread=name)
                    print(f"\n  NOK's {name} stopped unexpectedly. "
                          f"Knocks will no longer work; restart NOK.\n",
                          file=sys.stderr)
                    nok.set_state(ERROR, f"{name} stopped")

    threading.Thread(target=loop, daemon=True).start()


def _resolve_device(cal):
    try:
        return int(cal["device_index"])
    except (KeyError, TypeError, ValueError):
        return None


def main(argv=None):
    p = argparse.ArgumentParser(prog="nok", description="Knock-summoned desktop agent")
    p.add_argument("--calibrate", action="store_true", help="run the knock calibration wizard")
    p.add_argument("--calibrate-sides", action="store_true", help="calibrate left/right knocks")
    p.add_argument("--thump", action="store_true", help="SIMPLE mode: any loud thump wakes")
    p.add_argument("--device", type=int, default=None, help="input device index")
    p.add_argument("--no-sides", action="store_true", help="ignore side detection")
    p.add_argument("--no-screen", action="store_true", help="don't send screenshots")
    # --local is the name the README has always used for this. It was briefly a
    # silent no-op, which meant following the README exactly got you a different
    # mode than the one it described; it is a real alias again.
    p.add_argument("--chat", "--local", dest="chat", action="store_true",
                   help="conversational agent via Ollama, reads your screen, no key")
    p.add_argument("--realtime", action="store_true",
                   help="OpenAI Realtime voice agent (needs OPENAI_API_KEY + quota)")
    p.add_argument("--vision", action="store_true",
                   help="triple-knock reads your screen and answers in a "
                        "notification, no microphone (needs OPENAI_API_KEY)")
    p.add_argument("--fav", metavar="APP", default="",
                   help="bind the left double-knock to opening APP")
    p.add_argument("--speak", action="store_true",
                   help="speak a confirmation after each command")
    p.add_argument("--no-agent", action="store_true", help="instant actions only, no network")
    p.add_argument("--overlay", action="store_true",
                   help="show the on-screen HUD instead of notifications")
    args = p.parse_args(argv)

    if args.calibrate:
        from . import calibrate
        return calibrate.main()
    if args.calibrate_sides:
        from . import calibrate_sides
        return calibrate_sides.main()

    if args.realtime and not os.environ.get("OPENAI_API_KEY"):
        print("--realtime needs OPENAI_API_KEY. Falling back to spoken commands.\n")
        args.realtime = False
    if args.vision and not os.environ.get("OPENAI_API_KEY"):
        print("--vision needs OPENAI_API_KEY. Falling back to spoken commands.\n")
        args.vision = False
    if args.chat:
        from .local import pick_model
        if pick_model()[0] is None:
            print("--chat needs Ollama running with a model. "
                  "Falling back to spoken commands.\n")
            args.chat = False

    nok, detector, capture, ring, side_cfg = build(args)

    # Pay the screenshot stack's import + device-open cost now (~250ms) so the
    # first knock is as fast as every later one.
    from . import screen
    screen.warmup()

    # The default is no window at all: a tray icon, and a balloon when
    # something happens. The HUD is opt-in via --overlay.
    overlay = None
    tray = None
    if args.overlay:
        from .overlay import Overlay
        overlay = Overlay()
        nok.on_state = overlay.set_state
        nok.on_level = overlay.set_level
    else:
        from .notify import Tray
        tray = Tray()
        nok.on_notify = tray.balloon

    stop_event = threading.Event()
    ioi = IOIStateMachine(**ioi_settings())

    def _capture_died(reason):
        # SLEEPING renders as invisible, so without this a dead microphone is
        # pixel-identical to a healthy idle app -- you knock, nothing happens,
        # and nothing anywhere says why.
        print(f"\n  NOK has gone deaf: {reason}", file=sys.stderr)
        print("  The microphone stream stopped and could not be reopened.",
              file=sys.stderr)
        print("  Reconnect the mic (or free it from another app) and restart.\n",
              file=sys.stderr)
        nok.set_state(ERROR, "microphone lost")

    capture.on_error = _capture_died
    capture.start()
    detector_thread = threading.Thread(
        target=detector.run, args=(ring, ioi, stop_event),
        kwargs={"on_wake": nok.on_wake}, daemon=True,
    )
    detector_thread.start()

    agent_label = "read my screen" if args.vision else "speak a command"
    print("NOK is listening.\n")
    print(bindings.describe(side_cfg.enabled, agent_enabled=not args.no_agent,
                            fav_app=args.fav, agent_label=agent_label))
    if not args.no_agent and not args.chat and not args.realtime and not args.vision:
        print("\n  after a triple knock, say things like:")
        print('    "open spotify"        "take a screenshot"')
        print('    "pause"  "next track"  "volume up"  "mute"')
        print('    "type hello world"')
    if tray is not None and not tray.alive:
        print("\n  (no tray icon available -- confirmations print here instead)")
    print("\nCtrl-C to quit.")

    loop_thread = threading.Thread(target=lambda: asyncio.run(nok.run()), daemon=True)
    loop_thread.start()

    _watch_threads(nok, stop_event,
                   {"detector": detector_thread, "agent loop": loop_thread})

    try:
        if overlay is not None:
            overlay.run()
        elif tray is not None:
            tray.run()   # owns the Win32 message loop for the icon
        else:
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        nok.stop()
        stop_event.set()
        capture.stop()
        if overlay is not None:
            overlay.close()
        if tray is not None:
            tray.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
