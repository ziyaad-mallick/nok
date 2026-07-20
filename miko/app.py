"""Miko application state machine — the wiring that ties every subsystem into
one wake-to-act cycle.

    SLEEPING --double/triple tap (detector -> IOI)--> LISTENING
      LISTENING: wake chime (detector gated during playback + cooldown),
                 vad.endpoint captures the utterance;
                 5s no speech -> "back to sleep" chime -> SLEEPING
      -> transcribe; empty/garbled -> say what was heard, re-listen ONCE
      -> teach intent ("learn a new trick"/"teach you") -> TEACHING (teach.py)
      -> THINKING: brain.decide (detector BUSY-gated; reentrant knocks dropped
                   with one "still working"); QuotaExhausted -> notice once +
                   matcher fallback for the rest of the session; BrainTimeout /
                   ContractError -> error say -> SLEEPING
      -> ACTING: dispatcher runs the whitelisted action
      -> say the result (playback-gated) -> SLEEPING

Every transition is logged via log_event (the JSONL evidence trail) and pushed
to an optional observer(state, detail) callback so the E1 overlay can subscribe
later without touching this file. Heavy/hardware deps (sounddevice, faster-
whisper, onnxruntime, winsound) are imported lazily in build_app so importing
this module — and unit-testing the state machine with fakes — stays cheap.
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

from . import teach as teach_mod
from .brain import BrainTimeout, QuotaExhausted
from .ioi import TRIPLE_TAP
from .jsonx import ContractError
from .logs import log_event
from .tts import COOLDOWN_S, playback_gate
from .vad import NO_COMMAND

# States (plain strings so the overlay and the log read the same tokens).
SLEEPING = "SLEEPING"
LISTENING = "LISTENING"
THINKING = "THINKING"
ACTING = "ACTING"
TEACHING = "TEACHING"
ERROR = "ERROR"

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS_DIR = _REPO_ROOT / "skills"

# Triple-knock = instant quick action (no STT round-trip). Pass quick_action=None
# to disable.
DEFAULT_QUICK_ACTION = {"action": "media", "args": {"command": "mute"}, "say": "Muted."}


class App:
    def __init__(
        self,
        *,
        brain,
        dispatcher,
        matcher,
        loader,
        vad=None,
        transcriber=None,
        detector=None,
        frames=None,
        chime=None,
        speak=None,
        observer=None,
        teach_fn=teach_mod.teach,
        skills_dir=DEFAULT_SKILLS_DIR,
        min_confidence=0.4,
        cooldown=0.0,
        quick_action=DEFAULT_QUICK_ACTION,
    ):
        self.brain = brain
        self.dispatcher = dispatcher
        self.quick_action = quick_action
        self.matcher = matcher
        self.loader = loader
        self.vad = vad
        self.transcriber = transcriber
        self.detector = detector
        self.teach_fn = teach_fn
        self.skills_dir = Path(skills_dir)
        self.min_confidence = min_confidence
        self.cooldown = cooldown

        self._frames = frames or (lambda: iter(()))
        self.chime = chime or (lambda kind: None)
        self.speak = speak or (lambda text: None)
        self.observer = observer

        self.state = SLEEPING
        self._current_transcript = ""
        self._skill_manifest = self.loader.manifest_prompt()
        self._last_result = ""

        # session flags
        self._quota_exhausted = False
        self._quota_notified = False

        # BUSY: one wake cycle at a time; reentrant knocks are dropped.
        self._busy = False
        self._busy_notified = False

    # -- detector callback -------------------------------------------------

    def on_wake(self, pattern, t=None) -> str:
        """Called by the detector on a DOUBLE/TRIPLE tap. Drops the knock if a
        cycle is already running (design "BUSY state")."""
        if self._busy:
            log_event("wake_busy_drop", pattern=pattern, t=t)
            if not self._busy_notified:
                self._busy_notified = True
                self._say("Still working.")
            return "Still working on the last thing."
        return self.handle_wake(pattern)

    # -- the cycle ---------------------------------------------------------

    def handle_wake(self, pattern="DOUBLE_TAP") -> str:
        self._busy = True
        self._busy_notified = False
        try:
            return self._pipeline(pattern)
        except Exception as exc:
            # A crash in any subsystem (STT, teach codegen, dispatch) must not
            # unwind into detector.run() and kill the daemon thread -- that
            # leaves Miko silently deaf. Recover: one bad beat, then re-arm.
            log_event("pipeline_error", error=str(exc))
            self.set_state(ERROR, detail=f"pipeline error: {exc}")
            try:
                self._chime("error")
                self._say("Something went wrong.")
            except Exception:
                pass
            self._last_result = "pipeline error"
            return "pipeline error"
        finally:
            self._busy = False
            self._set_busy_gate(False)
            self.set_state(SLEEPING, detail="cycle complete")

    def _pipeline(self, pattern) -> str:
        self.set_state(LISTENING, detail=f"woke on {pattern}")
        self._chime("wake")

        # Triple knock = instant quick action, no listen/transcribe round-trip.
        if pattern == TRIPLE_TAP and self.quick_action is not None:
            self.set_state(ACTING, detail=self.quick_action.get("action"))
            result = self.dispatcher.dispatch(self.quick_action)
            self._say(result or self.quick_action.get("say", ""))
            return result

        # Hot-reload skills on every wake (design: loader scans on wake).
        self._refresh_skills()

        audio = self._listen()
        if audio is NO_COMMAND:
            # Log WHY: peak_rms near 0 => mic captured silence (device/gain);
            # peak_rms healthy but max_speech_score < threshold => speech was
            # there but the VAD scored it too low (lower MIKO_VAD_THRESHOLD).
            log_event("no_speech", stage="listen",
                      **getattr(self.vad, "last_stats", {}))
            self._chime("back_to_sleep")
            return "went back to sleep"

        text, info = self.transcriber.transcribe(audio)
        log_event("transcript", text=text,
                  confidence=round((info or {}).get("confidence", 0.0), 3))
        text, ok = self._resolve_transcript(text, info)
        if not ok:
            return "didn't catch that"

        self._current_transcript = text

        if teach_mod.is_teach_intent(text):
            self.set_state(TEACHING, detail=text)
            result = self.teach_fn(text, self.brain, self.loader, self.skills_dir)
            self._refresh_skills()  # make the new skill routable immediately
            self._say(result)
            return result

        self.set_state(THINKING, detail=text)
        action = self._decide(text)
        if action is None:
            return self._last_result  # error/fallback already spoke

        self.set_state(ACTING, detail=action.get("action"))
        result = self.dispatcher.dispatch(action)
        self._say(result or action.get("say", ""))
        return result

    # -- transcript handling ----------------------------------------------

    def _resolve_transcript(self, text, info):
        """Return (text, ok). Garbled/empty -> say what was heard, re-listen
        ONCE; still garbled -> (text, False)."""
        if not self._is_garbled(text, info):
            return text, True

        self._say(f"I heard: {text}" if (text or "").strip()
                  else "I didn't catch that.")
        audio = self._listen()
        if audio is NO_COMMAND:
            self._chime("back_to_sleep")
            return text, False

        text2, info2 = self.transcriber.transcribe(audio)
        if self._is_garbled(text2, info2):
            self._say(f"Still couldn't catch that: {text2}" if (text2 or "").strip()
                      else "Still didn't catch that.")
            return text2, False
        return text2, True

    def _is_garbled(self, text, info) -> bool:
        if not (text or "").strip():
            return True
        return (info or {}).get("confidence", 1.0) < self.min_confidence

    # -- decision ----------------------------------------------------------

    def _decide(self, text):
        """Return an action dict, or None (an error/fallback path already
        spoke and set self._last_result)."""
        self._set_busy_gate(True)
        try:
            if self._quota_exhausted:
                return self._fallback_action(text)

            context = {"skill_manifest": self._skill_manifest}
            try:
                return self.brain.decide(text, context)
            except QuotaExhausted:
                if not self._quota_notified:
                    self._quota_notified = True
                    self._say("I'm out of model quota, using offline commands.")
                self._quota_exhausted = True
                return self._fallback_action(text)
            except BrainTimeout:
                self.set_state(ERROR, detail="brain timed out")
                self._say("The brain timed out.")
                self._last_result = "brain timed out"
                return None
            except ContractError as exc:
                self.set_state(ERROR, detail=f"bad response: {exc}")
                self._say("I got a bad response from the brain.")
                self._last_result = "contract error"
                return None
        finally:
            self._set_busy_gate(False)

    def _fallback_action(self, text):
        action = self.matcher.match(text)
        if action is None:
            self._say("I couldn't match that to an offline command.")
            self._last_result = "no offline match"
            return None
        return action

    # -- skills ------------------------------------------------------------

    def _refresh_skills(self):
        try:
            self.loader.scan()
        except Exception as exc:  # a bad skill must never crash the cycle
            log_event("skill_scan_error", error=str(exc))
        self.loader.register_into(self.matcher)
        self._skill_manifest = self.loader.manifest_prompt()

    def run_skill(self, name) -> str:
        """Dispatcher skill hook: bridges the (name)->str signature to the
        loader, supplying the current transcript as ctx."""
        return self.loader.run_skill(name, self._current_transcript)

    # -- I/O helpers (playback-gated) -------------------------------------

    def _listen(self):
        return self.vad.endpoint(self._frames())

    def _say(self, text):
        if not text:
            return
        with playback_gate(on_start=lambda: self._set_playback(True),
                           on_end=lambda: self._set_playback(False),
                           cooldown=self.cooldown):
            self.speak(text)

    def _chime(self, kind):
        with playback_gate(on_start=lambda: self._set_playback(True),
                           on_end=lambda: self._set_playback(False),
                           cooldown=self.cooldown):
            self.chime(kind)

    def _set_playback(self, on):
        if self.detector is None:
            return
        try:
            self.detector.set_playback_gate(on)
        except AttributeError:
            self.detector.playback_gate = on

    def _set_busy_gate(self, on):
        if self.detector is None:
            return
        try:
            self.detector.set_busy_gate(on)
        except AttributeError:
            self.detector.busy_gate = on

    # -- observer ----------------------------------------------------------

    def set_state(self, state, detail=""):
        self.state = state
        log_event("state", state=state, detail=detail)
        if self.observer is not None:
            try:
                self.observer(state, detail)
            except Exception as exc:  # an overlay bug must not break the cycle
                log_event("observer_error", error=str(exc))


# ==========================================================================
# Production wiring (not exercised by unit tests — touches real hardware).
# ==========================================================================

def _make_chime():
    """winsound-based chimes; a distinct arpeggio per state cue."""
    import winsound

    tunes = {
        "wake": [(660, 90), (990, 120)],
        "back_to_sleep": [(660, 90), (440, 120)],
        "error": [(330, 200)],
    }

    def chime(kind):
        try:
            for freq, dur in tunes.get(kind, [(660, 100)]):
                winsound.Beep(freq, dur)
        except Exception as exc:
            log_event("chime_error", kind=kind, error=str(exc))

    return chime


def _make_speak():
    from .tts import say

    def speak(text):
        say(text)  # tts.say owns its own timeout/kill; gating is applied by App

    return speak


def _make_frame_source(ring_buffer, detector, stop_event):
    """Yield contiguous 512-sample blocks from the ring buffer for
    vad.endpoint(). Sub-block remainders are carried across reads (not dropped)
    so the stream stays gapless -- Silero's recurrent state depends on
    contiguous frames, and dropped samples were degrading detection."""
    import numpy as np

    from .ring_buffer import SAMPLE_RATE

    bs = 512

    def frames():
        consumed = ring_buffer.total_written
        leftover = np.zeros(0, dtype=np.float32)
        while not stop_event.is_set():
            total = ring_buffer.total_written
            new = total - consumed
            if new <= 0:
                time.sleep(0.005)
                continue
            new = min(new, ring_buffer.capacity)  # never ask for more than retained
            chunk = ring_buffer.read_last(new / SAMPLE_RATE)
            consumed = total
            buf = np.concatenate([leftover, chunk]) if leftover.size else chunk
            n_full = (buf.size // bs) * bs
            for i in range(0, n_full, bs):
                yield buf[i:i + bs]
            leftover = buf[n_full:].copy()

    return frames


def build_app(args):
    """Wire the real subsystems for `python -m miko`."""
    from .actions import Actions
    from .audio import CaptureLoop
    from .config import load_config
    from .detector import DetectorConfig, KnockDetector
    from .dispatcher import Dispatcher
    from .matcher import build_default_matcher
    from .ring_buffer import RingBuffer
    from .skills_loader import SkillsLoader
    from .stt import Transcriber
    from .vad import get_vad
    from . import helpers as helpers_module
    from .brain import CodexBrain, MockBrain

    apps, macros = load_config()
    actions = Actions(apps, macros)
    helpers_module.bind(actions)  # skills reach the whitelist through helpers

    brain = CodexBrain() if args.real else MockBrain()
    matcher = build_default_matcher(apps)
    loader = SkillsLoader(skills_dir=DEFAULT_SKILLS_DIR, brain=brain)
    loader.scan()
    loader.register_into(matcher)

    # detector config: calibration file if present, --thump forces SIMPLE mode
    cal = _REPO_ROOT / "calibration.json"
    if cal.exists():
        det_cfg = DetectorConfig.from_calibration_file(str(cal))
    else:
        det_cfg = DetectorConfig()
    if args.thump:
        det_cfg.simple_mode = True
        det_cfg.simple_amplitude = float(
            os.environ.get("MIKO_THUMP_AMPLITUDE", det_cfg.simple_amplitude)
        )
    detector = KnockDetector(det_cfg)

    ring_buffer = RingBuffer(seconds=10.0)
    vad = get_vad()
    transcriber = Transcriber()

    stop_event = threading.Event()
    frames = _make_frame_source(ring_buffer, detector, stop_event)

    app = App(
        brain=brain,
        dispatcher=None,  # set below once run_skill is bound
        matcher=matcher,
        loader=loader,
        vad=vad,
        transcriber=transcriber,
        detector=detector,
        frames=frames,
        chime=_make_chime(),
        speak=_make_speak(),
        cooldown=COOLDOWN_S,
    )
    app.dispatcher = Dispatcher(actions, skill_runner=app.run_skill)

    device = args.device if args.device is not None else _resolve_device(cal)
    capture = CaptureLoop(ring_buffer, device=device)
    return app, detector, capture, ring_buffer, stop_event


def _resolve_device(cal_path):
    import json
    if Path(cal_path).exists():
        try:
            return int(json.loads(Path(cal_path).read_text())["device_index"])
        except Exception:
            pass
    return None  # sounddevice default input device


def main(argv=None):
    parser = argparse.ArgumentParser(prog="miko", description="Knock-summoned desktop agent")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", default=True,
                       help="use the MockBrain (default; zero spend)")
    group.add_argument("--real", action="store_true",
                       help="use the CodexBrain (spends model quota)")
    parser.add_argument("--thump", action="store_true",
                        help="SIMPLE mode: any loud chassis thump wakes Miko")
    parser.add_argument("--device", type=int, default=None,
                        help="override the input device index")
    parser.add_argument("--calibrate", action="store_true",
                        help="run the knock calibration wizard and exit")
    parser.add_argument("--no-overlay", action="store_true",
                        help="run headless without the on-screen overlay")
    args = parser.parse_args(argv)

    if args.calibrate:
        from . import calibrate
        return calibrate.main()

    app, detector, capture, ring_buffer, stop_event = build_app(args)
    from .ioi import IOIStateMachine

    ioi = IOIStateMachine()
    capture.start()
    worker = threading.Thread(
        target=detector.run,
        args=(ring_buffer, ioi, stop_event),
        kwargs={"on_wake": app.on_wake},
        daemon=True,
    )
    worker.start()

    overlay = None
    if not args.no_overlay:
        from .overlay import Overlay  # lazy import (keeps headless imports cheap)
        overlay = Overlay()
        app.observer = overlay.set_state  # App already supports observer

    app.set_state(SLEEPING, detail="ready — knock twice")
    print("Miko is listening. Knock twice on the chassis. Ctrl-C to quit.")
    try:
        if overlay is not None:
            overlay.run()
        else:
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        capture.stop()
        if overlay is not None:
            overlay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
