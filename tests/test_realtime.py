"""RealtimeSession event handling, without a network or a sound card.

The live API cannot be exercised in CI, but almost every bug in this layer is
in the event handling and the barge-in sequence, both of which are pure logic
over a connection object. Mock the connection and the audio devices; test the
logic properly.
"""
import base64

import numpy as np
import pytest

from nok.realtime import SAMPLE_RATE, AudioPlayer, RealtimeSession


# -- fakes ----------------------------------------------------------------

class FakeEvent:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class FakeItemNS:
    """Mimics conn.conversation.item.create / conn.response.create etc."""

    def __init__(self, log, path):
        self._log = log
        self._path = path

    async def create(self, **kw):
        self._log.append((self._path + ".create", kw))

    async def update(self, **kw):
        self._log.append((self._path + ".update", kw))

    async def append(self, **kw):
        self._log.append((self._path + ".append", kw))

    async def commit(self, **kw):
        self._log.append((self._path + ".commit", kw))


class FakeConn:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    @property
    def session(self):
        return FakeItemNS(self.sent, "session")

    @property
    def response(self):
        return FakeItemNS(self.sent, "response")

    @property
    def input_audio_buffer(self):
        return FakeItemNS(self.sent, "input_audio_buffer")

    @property
    def conversation(self):
        outer = self

        class _Conv:
            @property
            def item(self):
                return FakeItemNS(outer.sent, "conversation.item")
        return _Conv()

    def types_sent(self):
        out = []
        for entry in self.sent:
            if isinstance(entry, dict):
                out.append(entry.get("type"))
            else:
                out.append(entry[0])
        return out


def _session(**kw):
    s = RealtimeSession(api_key="test", **kw)
    # never touch real hardware in tests
    s.player = _FakePlayer()
    return s


class _FakePlayer:
    def __init__(self):
        self.chunks = []
        self.flushed = 0
        self.level = 0.0
        self._played = 0

    def add(self, pcm):
        self.chunks.append(pcm)
        self._played += len(pcm) // 2

    def played_ms(self):
        return int(self._played / SAMPLE_RATE * 1000)

    def flush(self):
        self.flushed += 1

    def close(self):
        pass


def _pcm(n=480, amp=1000):
    return (np.ones(n, dtype=np.int16) * amp).tobytes()


def _b64(b):
    return base64.b64encode(b).decode("ascii")


# -- AudioPlayer ----------------------------------------------------------

def test_player_starts_silent():
    p = AudioPlayer()
    assert p.frames_played == 0
    assert p.played_ms() == 0


def test_player_callback_drains_the_queue_in_order():
    p = AudioPlayer()
    with p._lock:
        p._queue.append(np.arange(10, dtype=np.int16))
        p._queue.append(np.arange(10, 20, dtype=np.int16))
    out = np.zeros((15, 1), dtype=np.int16)
    p._callback(out, 15, None, None)
    assert list(out[:, 0]) == list(range(15))
    # the unconsumed tail is kept for the next callback
    with p._lock:
        assert list(p._queue[0]) == list(range(15, 20))


def test_player_zero_pads_on_underrun_instead_of_starving_the_device():
    p = AudioPlayer()
    with p._lock:
        p._queue.append(np.ones(4, dtype=np.int16) * 7)
    out = np.full((10, 1), 99, dtype=np.int16)
    p._callback(out, 10, None, None)
    assert list(out[:, 0]) == [7, 7, 7, 7, 0, 0, 0, 0, 0, 0]


def test_player_only_counts_frames_it_actually_played():
    """played_ms drives barge-in truncation — counting silence would tell the
    model the user heard audio that was never produced."""
    p = AudioPlayer()
    out = np.zeros((100, 1), dtype=np.int16)
    p._callback(out, 100, None, None)   # nothing queued: pure underrun
    assert p.frames_played == 0


def test_flush_drops_queued_audio():
    p = AudioPlayer()
    with p._lock:
        p._queue.append(np.ones(100, dtype=np.int16))
    p.flush()
    assert p._queue == []
    assert p.level == 0.0


# -- event handling -------------------------------------------------------

async def test_audio_delta_reaches_the_player():
    s = _session()
    conn = FakeConn()
    await s._handle(conn, FakeEvent("response.output_audio.delta",
                                    delta=_b64(_pcm()), item_id="item_1"))
    assert len(s.player.chunks) == 1


async def test_first_audio_latency_is_recorded_once():
    s = _session()
    conn = FakeConn()
    s._woke_at = 0.0
    for _ in range(3):
        await s._handle(conn, FakeEvent("response.output_audio.delta",
                                        delta=_b64(_pcm()), item_id="i"))
    first = s.stats.first_audio_latency
    assert first > 0
    await s._handle(conn, FakeEvent("response.output_audio.delta",
                                    delta=_b64(_pcm()), item_id="i"))
    assert s.stats.first_audio_latency == first


async def test_states_follow_the_conversation():
    seen = []
    s = _session(on_state=lambda st, d="": seen.append(st))
    conn = FakeConn()
    await s._handle(conn, FakeEvent("input_audio_buffer.speech_started"))
    await s._handle(conn, FakeEvent("input_audio_buffer.speech_stopped"))
    await s._handle(conn, FakeEvent("response.output_audio.delta",
                                    delta=_b64(_pcm()), item_id="i"))
    await s._handle(conn, FakeEvent("response.done"))
    assert seen == ["LISTENING", "THINKING", "SPEAKING", "LISTENING"]


async def test_user_speech_flushes_playback_so_nok_stops_talking():
    s = _session()
    conn = FakeConn()
    await s._handle(conn, FakeEvent("input_audio_buffer.speech_started"))
    assert s.player.flushed == 1


async def test_response_done_counts_a_turn():
    s = _session()
    conn = FakeConn()
    await s._handle(conn, FakeEvent("response.done"))
    await s._handle(conn, FakeEvent("response.done"))
    assert s.stats.turns == 2


async def test_errors_arrive_as_events_and_are_surfaced_not_swallowed():
    """The SDK does not raise on API errors — unhandled, they are silent."""
    seen = []
    s = _session(on_state=lambda st, d="": seen.append(st))
    conn = FakeConn()
    err = FakeEvent("error", error=FakeEvent("x", message="session expired"))
    await s._handle(conn, err)
    assert "session expired" in s.stats.errors
    assert "ERROR" in seen


async def test_unknown_events_are_ignored_quietly():
    s = _session()
    conn = FakeConn()
    await s._handle(conn, FakeEvent("rate_limits.updated"))
    await s._handle(conn, FakeEvent("some.future.event"))
    assert s.stats.errors == []


# -- barge-in -------------------------------------------------------------

async def test_interrupt_cancels_truncates_and_flushes():
    """All three are required: cancel alone leaves the server generating,
    flush alone leaves the model believing it was fully heard."""
    s = _session()
    conn = FakeConn()
    s._conn = conn
    await s._handle(conn, FakeEvent("response.output_audio.delta",
                                    delta=_b64(_pcm()), item_id="item_42"))
    await s.interrupt()

    kinds = [p.get("type") for p in conn.sent if isinstance(p, dict)]
    assert "response.cancel" in kinds
    trunc = [p for p in conn.sent
             if isinstance(p, dict) and p.get("type") == "conversation.item.truncate"]
    assert trunc and trunc[0]["item_id"] == "item_42"
    assert s.player.flushed >= 1


async def test_interrupt_reports_how_much_audio_was_heard():
    s = _session()
    conn = FakeConn()
    s._conn = conn
    await s._handle(conn, FakeEvent("response.output_audio.delta",
                                    delta=_b64(_pcm(n=SAMPLE_RATE)), item_id="i"))
    await s.interrupt()
    trunc = [p for p in conn.sent
             if isinstance(p, dict) and p.get("type") == "conversation.item.truncate"][0]
    assert trunc["audio_end_ms"] == pytest.approx(1000, abs=50)


async def test_interrupt_is_a_noop_when_nothing_is_speaking():
    s = _session()
    conn = FakeConn()
    s._conn = conn
    await s.interrupt()
    assert conn.sent == []


# -- tool calls -----------------------------------------------------------

async def test_media_tool_taps_the_key_and_reports_back():
    s = _session()
    conn = FakeConn()
    tapped = []
    import nok.actions as actions
    orig = actions._tap_vk
    actions._tap_vk = lambda vk: tapped.append(vk)
    try:
        await s._handle(conn, FakeEvent("response.function_call_arguments.done",
                                        name="media_control", call_id="c1",
                                        arguments='{"command": "play"}'))
    finally:
        actions._tap_vk = orig

    assert tapped == [actions.VK_MEDIA_PLAY_PAUSE]
    outs = [p for _, p in conn.sent if isinstance(p, dict) and "item" in p]
    assert any(i["item"].get("call_id") == "c1" for i in outs)


async def test_unknown_tool_still_returns_an_output_so_the_turn_completes():
    """A tool call with no output leaves the model waiting forever."""
    s = _session()
    conn = FakeConn()
    await s._handle(conn, FakeEvent("response.function_call_arguments.done",
                                    name="nonexistent", call_id="c9",
                                    arguments="{}"))
    outs = [p for _, p in conn.sent if isinstance(p, dict) and "item" in p]
    assert any(i["item"].get("call_id") == "c9" for i in outs)


async def test_malformed_tool_arguments_do_not_crash_the_turn():
    s = _session()
    conn = FakeConn()
    await s._handle(conn, FakeEvent("response.function_call_arguments.done",
                                    name="media_control", call_id="c2",
                                    arguments="{not json"))
    assert "response.create" in [k for k, _ in conn.sent]


# -- config ---------------------------------------------------------------

async def test_session_config_requests_pcm16_at_24k_both_ways():
    s = _session()
    conn = FakeConn()
    await s._configure(conn)
    _, kw = conn.sent[0]
    audio = kw["session"]["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio["output"]["format"] == {"type": "audio/pcm", "rate": 24000}


async def test_server_vad_is_enabled_so_it_stays_a_conversation():
    """Manual turn detection would force a gesture to end every single turn."""
    s = _session()
    conn = FakeConn()
    await s._configure(conn)
    _, kw = conn.sent[0]
    td = kw["session"]["audio"]["input"]["turn_detection"]
    assert td["type"] == "server_vad"
    assert td["interrupt_response"] is True


async def test_tools_are_declared():
    s = _session()
    conn = FakeConn()
    await s._configure(conn)
    _, kw = conn.sent[0]
    names = {t["name"] for t in kw["session"]["tools"]}
    assert {"take_screenshot", "media_control"} <= names


# -- screen attach --------------------------------------------------------

async def test_screenshot_is_attached_as_an_input_image():
    import asyncio
    s = _session()
    conn = FakeConn()
    fut = asyncio.get_event_loop().create_future()
    fut.set_result("data:image/jpeg;base64,AAAA")
    await s._attach_screen(conn, fut, None)

    items = [p for _, p in conn.sent if isinstance(p, dict) and "item" in p]
    assert items
    parts = items[0]["item"]["content"]
    assert parts[0]["type"] == "input_image"
    assert parts[0]["image_url"].startswith("data:image/jpeg;base64,")


async def test_a_failed_screenshot_does_not_break_the_session():
    import asyncio
    s = _session()
    conn = FakeConn()
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(None)
    await s._attach_screen(conn, fut, None)
    assert conn.sent == []
