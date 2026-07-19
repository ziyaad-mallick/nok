"""App state-machine tests. Every component is faked: no audio devices, no
codex, no real TTS. Aims at each branch of the wake->act cycle."""
import pytest

from miko.app import ACTING, ERROR, LISTENING, SLEEPING, THINKING, App
from miko.brain import BrainTimeout, QuotaExhausted
from miko.jsonx import ContractError
from miko.matcher import build_default_matcher

AUDIO = object()  # opaque "utterance captured" sentinel
GOOD = {"confidence": 0.9}
BAD = {"confidence": 0.1}

APPS = {"spotify": {"launch": "spotify:"}}


class FakeVad:
    def __init__(self, results):
        self._results = list(results)

    def endpoint(self, frames):
        return self._results.pop(0)


class FakeTranscriber:
    def __init__(self, results):
        self._results = list(results)

    def transcribe(self, audio):
        return self._results.pop(0)


class FakeBrain:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.decide_calls = 0

    def decide(self, text, context=None):
        self.decide_calls += 1
        out = self._outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class FakeDispatcher:
    def __init__(self, result="did it"):
        self.result = result
        self.dispatched = []

    def dispatch(self, action):
        self.dispatched.append(action)
        return self.result


class FakeDetector:
    def __init__(self):
        self.playback_gate = False
        self.busy_gate = False

    def set_playback_gate(self, on):
        self.playback_gate = on

    def set_busy_gate(self, on):
        self.busy_gate = on


class FakeLoader:
    def scan(self):
        return []

    def register_into(self, matcher):
        pass

    def manifest_prompt(self):
        return "(no learned skills yet)"

    def run_skill(self, name, transcript):
        return f"ran {name}"


def make_app(*, vad, transcriber=None, brain=None, dispatcher=None, matcher=None,
             teach_fn=None, loader=None):
    states = []
    chimes = []
    spoken = []
    detector = FakeDetector()
    app = App(
        brain=brain or FakeBrain([]),
        dispatcher=dispatcher or FakeDispatcher(),
        matcher=matcher if matcher is not None else build_default_matcher(APPS),
        loader=loader or FakeLoader(),
        vad=vad,
        transcriber=transcriber or FakeTranscriber([]),
        detector=detector,
        frames=lambda: iter(()),
        chime=lambda kind: chimes.append(kind),
        speak=lambda text: spoken.append(text),
        observer=lambda s, d: states.append(s),
        teach_fn=teach_fn or (lambda *a, **k: "taught"),
        cooldown=0.0,
    )
    return app, states, chimes, spoken, detector


# -- happy path -----------------------------------------------------------

def test_wake_listen_act_happy_path():
    action = {"action": "open_app", "args": {"name": "spotify"}, "say": "Opening."}
    disp = FakeDispatcher(result="Opening spotify.")
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("open spotify", GOOD)]),
        brain=FakeBrain([action]),
        dispatcher=disp,
    )
    result = app.handle_wake("DOUBLE_TAP")

    assert result == "Opening spotify."
    assert disp.dispatched == [action]
    assert "wake" in chimes
    assert states == [LISTENING, THINKING, ACTING, SLEEPING]
    assert app.state == SLEEPING


# -- pipeline crash recovery (S1: detector thread must survive) -----------

class _RaisingTranscriber:
    def transcribe(self, audio):
        raise RuntimeError("stt exploded")


def test_pipeline_exception_recovers_and_returns_to_sleeping():
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=_RaisingTranscriber(),
    )
    # Must NOT raise: handle_wake runs on the detector thread, and an uncaught
    # exception there kills it (Miko goes silently deaf).
    result = app.handle_wake("DOUBLE_TAP")
    assert result == "pipeline error"
    assert app.state == SLEEPING
    assert ERROR in states
    assert states[-1] == SLEEPING
    assert "error" in chimes


def test_teach_exception_recovers():
    def boom(*a, **k):
        raise RuntimeError("teach exploded")

    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("learn a new trick: do x", GOOD)]),
        teach_fn=boom,
    )
    result = app.handle_wake("DOUBLE_TAP")
    assert result == "pipeline error"
    assert app.state == SLEEPING
    assert ERROR in states


# -- 5s no-speech ---------------------------------------------------------

def test_no_speech_goes_back_to_sleep():
    from miko.vad import NO_COMMAND
    disp = FakeDispatcher()
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([NO_COMMAND]),
        transcriber=FakeTranscriber([]),  # never called
        brain=FakeBrain([]),
        dispatcher=disp,
    )
    result = app.handle_wake()
    assert result == "went back to sleep"
    assert chimes == ["wake", "back_to_sleep"]
    assert disp.dispatched == []


# -- BUSY knock dropped ---------------------------------------------------

def test_busy_knock_dropped_with_one_notice():
    captured = {}

    def reenter(text, context=None):
        # a knock arrives mid-decide: it must be dropped
        captured["drop"] = app.on_wake("DOUBLE_TAP")
        return {"action": "media", "args": {"command": "mute"}, "say": "Muting."}

    brain = FakeBrain([])
    brain.decide = reenter  # type: ignore
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("mute", GOOD)]),
        brain=brain,
    )
    app.handle_wake()
    assert captured["drop"] == "Still working on the last thing."


def test_busy_gate_set_during_thinking():
    seen = {}

    def check_gate(text, context=None):
        seen["busy"] = app.detector.busy_gate
        return {"action": "media", "args": {"command": "mute"}, "say": "Muting."}

    brain = FakeBrain([])
    brain.decide = check_gate  # type: ignore
    app, *_ = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("mute", GOOD)]),
        brain=brain,
    )
    app.handle_wake()
    assert seen["busy"] is True
    assert app.detector.busy_gate is False  # released after the cycle


# -- quota -> session fallback via matcher --------------------------------

def test_quota_falls_back_to_matcher_for_the_session():
    disp = FakeDispatcher(result="Muting.")
    brain = FakeBrain([QuotaExhausted("mock")])  # raises once; then never called
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO, AUDIO]),
        transcriber=FakeTranscriber([("mute the music", GOOD), ("mute the music", GOOD)]),
        brain=brain,
        dispatcher=disp,
    )
    # first wake: quota hit -> notice + matcher fallback
    r1 = app.handle_wake()
    # second wake: session flag set -> matcher directly, brain NOT called again
    r2 = app.handle_wake()

    assert brain.decide_calls == 1
    assert disp.dispatched[0]["action"] == "media"
    assert len(disp.dispatched) == 2
    notices = [s for s in spoken if "quota" in s.lower()]
    assert len(notices) == 1  # spoken exactly once


def test_quota_fallback_no_match_says_so():
    brain = FakeBrain([QuotaExhausted("mock")])
    disp = FakeDispatcher()
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("the weather is nice", GOOD)]),
        brain=brain,
        dispatcher=disp,
    )
    result = app.handle_wake()
    assert result == "no offline match"
    assert disp.dispatched == []


# -- timeout / contract error --------------------------------------------

def test_timeout_errors_and_returns_to_sleeping():
    disp = FakeDispatcher()
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("open spotify", GOOD)]),
        brain=FakeBrain([BrainTimeout("mock")]),
        dispatcher=disp,
    )
    result = app.handle_wake()
    assert result == "brain timed out"
    assert ERROR in states
    assert states[-1] == SLEEPING
    assert disp.dispatched == []


def test_contract_error_errors_and_returns_to_sleeping():
    disp = FakeDispatcher()
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("gibberish", GOOD)]),
        brain=FakeBrain([ContractError("bad json")]),
        dispatcher=disp,
    )
    result = app.handle_wake()
    assert result == "contract error"
    assert ERROR in states
    assert disp.dispatched == []


# -- garbled re-listen once ----------------------------------------------

def test_garbled_then_good_uses_second_transcript():
    action = {"action": "media", "args": {"command": "mute"}, "say": "Muting."}
    disp = FakeDispatcher(result="Muting.")
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO, AUDIO]),  # first capture, then the re-listen
        transcriber=FakeTranscriber([("", BAD), ("mute", GOOD)]),
        brain=FakeBrain([action]),
        dispatcher=disp,
    )
    result = app.handle_wake()
    assert result == "Muting."
    assert disp.dispatched == [action]


def test_garbled_twice_gives_up():
    disp = FakeDispatcher()
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO, AUDIO]),
        transcriber=FakeTranscriber([("mmmf", BAD), ("mmmf", BAD)]),
        brain=FakeBrain([]),
        dispatcher=disp,
    )
    result = app.handle_wake()
    assert result == "didn't catch that"
    assert disp.dispatched == []


def test_garbled_then_no_speech_gives_up():
    from miko.vad import NO_COMMAND
    disp = FakeDispatcher()
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO, NO_COMMAND]),
        transcriber=FakeTranscriber([("", BAD)]),
        brain=FakeBrain([]),
        dispatcher=disp,
    )
    result = app.handle_wake()
    assert result == "didn't catch that"
    assert disp.dispatched == []


# -- teach intent routes to teach, not the dispatcher --------------------

def test_teach_intent_routes_to_teach():
    calls = {}

    def fake_teach(transcript, brain, loader, skills_dir):
        calls["transcript"] = transcript
        return "Learned 'focus_mode'."

    disp = FakeDispatcher()
    app, states, chimes, spoken, det = make_app(
        vad=FakeVad([AUDIO]),
        transcriber=FakeTranscriber([("miko learn a new trick focus mode", GOOD)]),
        brain=FakeBrain([]),  # decide must never be called
        dispatcher=disp,
        teach_fn=fake_teach,
    )
    result = app.handle_wake()
    assert result == "Learned 'focus_mode'."
    assert "learn a new trick" in calls["transcript"]
    assert disp.dispatched == []
    assert "Learned 'focus_mode'." in spoken
