"""The v2 orchestrator: gesture queue in, actions out.

The defect these guard against is v1's: the wake pipeline ran synchronously
inside the detector's loop, so MIKO was deaf while working. Here the detector
only ever pushes to a queue, which is what makes knock-to-interrupt possible.
"""
import asyncio

import pytest

from miko.app import ACTING, ERROR, SLEEPING, SPEAKING, Gesture, Miko
from miko.ioi import DOUBLE_TAP, TRIPLE_TAP, Decision
from miko.side import LEFT, RIGHT, UNKNOWN


class FakeDetector:
    def __init__(self):
        self.playback_gate = False
        self.gate_history = []

    def set_playback_gate(self, on):
        self.playback_gate = on
        self.gate_history.append(on)


class FakeSession:
    """Stands in for RealtimeSession without touching audio or the network."""

    def __init__(self, on_state=None, runtime=0.05):
        self.on_state = on_state or (lambda s, d="": None)
        self.runtime = runtime
        self.stopped = False
        self.ran = False

    def request_stop(self):
        self.stopped = True

    async def run(self):
        self.ran = True
        self.on_state(SPEAKING, "talking")
        await asyncio.sleep(self.runtime)
        self.on_state("LISTENING", "your turn")

        class Stats:
            turns = 1
            first_audio_latency = 0.3
        return Stats()


def make(**kw):
    states = []
    miko = Miko(
        detector=kw.pop("detector", FakeDetector()),
        sides_enabled=kw.pop("sides_enabled", True),
        on_state=lambda s, d="": states.append(s),
        **kw,
    )
    return miko, states


async def drain(miko, states=None, ticks=400):
    """Run the loop until the gesture has been fully handled.

    An empty queue is NOT enough: the gesture is dequeued before `_handle`
    runs, and handlers deliberately linger (confirmation flourish, error
    recovery). Wait for the loop to come back to rest instead.
    """
    task = asyncio.create_task(miko.run())
    for _ in range(ticks):
        await asyncio.sleep(0.01)
        settled = (
            miko.gestures.empty()
            and miko._session is None
            and miko.state == SLEEPING
            and (states is None or any(s != SLEEPING for s in states))
        )
        if settled:
            break
    miko.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# -- the detector callback must never block -------------------------------

def test_on_wake_returns_immediately_and_only_enqueues():
    miko, _ = make()
    miko.on_wake(Decision(DOUBLE_TAP, RIGHT), 1.0)
    assert miko.gestures.qsize() == 1
    assert miko.gestures.get().side == RIGHT


def test_on_wake_survives_a_plain_string_pattern():
    """detector._emit may pass a bare str if side detection is off."""
    miko, _ = make()
    miko.on_wake(DOUBLE_TAP, 1.0)
    assert miko.gestures.get().side == UNKNOWN


def test_queue_accepts_knocks_faster_than_they_are_handled():
    miko, _ = make()
    for i in range(10):
        miko.on_wake(Decision(DOUBLE_TAP, RIGHT), float(i))
    assert miko.gestures.qsize() == 10


# -- instant actions ------------------------------------------------------

@pytest.mark.asyncio
async def test_double_right_takes_a_screenshot_without_the_network():
    calls = []
    miko, states = make(session_factory=None)
    miko._screenshot = lambda: calls.append("shot") or "C:/shot.png"

    miko.on_wake(Decision(DOUBLE_TAP, RIGHT), 1.0)
    await drain(miko, states)

    assert calls == ["shot"]
    assert ACTING in states
    assert states[-1] == SLEEPING


@pytest.mark.asyncio
async def test_double_left_hits_play_pause():
    calls = []
    miko, _ = make(session_factory=None)
    miko._play_pause = lambda: calls.append("pp") or "play/pause"

    miko.on_wake(Decision(DOUBLE_TAP, LEFT), 1.0)
    await drain(miko)

    assert calls == ["pp"]


@pytest.mark.asyncio
async def test_instant_action_never_builds_a_session():
    built = []
    miko, _ = make(session_factory=lambda on_state: built.append(1))
    miko._screenshot = lambda: "x"

    miko.on_wake(Decision(DOUBLE_TAP, RIGHT), 1.0)
    await drain(miko)

    assert built == []


@pytest.mark.asyncio
async def test_a_failing_instant_action_recovers_to_sleeping():
    def boom():
        raise OSError("no display")

    miko, states = make(session_factory=None)
    miko._screenshot = boom

    miko.on_wake(Decision(DOUBLE_TAP, RIGHT), 1.0)
    await drain(miko, states)

    assert ERROR in states
    assert states[-1] == SLEEPING


# -- the agent session ----------------------------------------------------

@pytest.mark.asyncio
async def test_triple_opens_a_session():
    made = []

    def factory(on_state):
        s = FakeSession(on_state)
        made.append(s)
        return s

    miko, _ = make(session_factory=factory)
    miko.on_wake(Decision(TRIPLE_TAP, UNKNOWN), 1.0)
    await drain(miko)

    assert len(made) == 1 and made[0].ran


@pytest.mark.asyncio
async def test_a_knock_during_a_live_session_closes_it():
    """The whole point of the queue architecture."""
    made = []
    miko, _ = make(session_factory=lambda on_state: (
        made.append(FakeSession(on_state, runtime=5.0)) or made[-1]
    ))

    miko.on_wake(Decision(TRIPLE_TAP, UNKNOWN), 1.0)
    task = asyncio.create_task(miko.run())
    await asyncio.sleep(0.08)
    assert miko._session is not None, "session should be live"

    miko.on_wake(Decision(DOUBLE_TAP, RIGHT), 2.0)
    await asyncio.sleep(0.1)

    assert made[0].stopped is True
    assert miko._session is None
    miko.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_any_pattern_closes_a_live_session():
    """Interrupting must not require aiming at a particular gesture."""
    for pattern, side in [(DOUBLE_TAP, LEFT), (DOUBLE_TAP, RIGHT), (TRIPLE_TAP, UNKNOWN)]:
        made = []
        miko, _ = make(session_factory=lambda on_state: (
            made.append(FakeSession(on_state, runtime=5.0)) or made[-1]
        ))
        miko.on_wake(Decision(TRIPLE_TAP, UNKNOWN), 1.0)
        task = asyncio.create_task(miko.run())
        await asyncio.sleep(0.08)

        miko.on_wake(Decision(pattern, side), 2.0)
        await asyncio.sleep(0.1)

        assert made[0].stopped is True, f"{pattern}/{side} failed to close"
        miko.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_closing_knock_does_not_also_fire_its_own_action():
    """Knocking to stop the agent must not additionally take a screenshot."""
    shots = []
    miko, _ = make(session_factory=lambda on_state: FakeSession(on_state, runtime=5.0))
    miko._screenshot = lambda: shots.append(1) or "x"

    miko.on_wake(Decision(TRIPLE_TAP, UNKNOWN), 1.0)
    task = asyncio.create_task(miko.run())
    await asyncio.sleep(0.08)
    miko.on_wake(Decision(DOUBLE_TAP, RIGHT), 2.0)
    await asyncio.sleep(0.1)

    assert shots == []
    miko.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_triple_without_an_agent_configured_errors_then_recovers():
    miko, states = make(session_factory=None)
    miko.on_wake(Decision(TRIPLE_TAP, UNKNOWN), 1.0)
    await drain(miko, states)

    assert ERROR in states
    assert states[-1] == SLEEPING


# -- self-hearing (v1's fix, carried forward) -----------------------------

@pytest.mark.asyncio
async def test_the_detector_is_gated_while_the_agent_speaks():
    det = FakeDetector()
    miko, states = make(detector=det,
                        session_factory=lambda on_state: FakeSession(on_state))

    miko.on_wake(Decision(TRIPLE_TAP, UNKNOWN), 1.0)
    await drain(miko, states)

    assert True in det.gate_history, "gate never closed during SPEAKING"
    assert det.playback_gate is False, "gate left closed after the session"


@pytest.mark.asyncio
async def test_the_gate_is_released_even_if_the_session_raises():
    class Exploding(FakeSession):
        async def run(self):
            self.on_state(SPEAKING, "talking")
            raise RuntimeError("socket died")

    det = FakeDetector()
    miko, states = make(detector=det,
                        session_factory=lambda on_state: Exploding(on_state))

    miko.on_wake(Decision(TRIPLE_TAP, UNKNOWN), 1.0)
    await drain(miko, states)

    assert det.playback_gate is False
    assert ERROR in states


# -- degraded gesture map -------------------------------------------------

@pytest.mark.asyncio
async def test_left_double_takes_a_screenshot_when_sides_are_off():
    shots, pp = [], []
    miko, _ = make(sides_enabled=False, session_factory=None)
    miko._screenshot = lambda: shots.append(1) or "x"
    miko._play_pause = lambda: pp.append(1) or "y"

    miko.on_wake(Decision(DOUBLE_TAP, LEFT), 1.0)
    await drain(miko)

    assert shots == [1] and pp == []
