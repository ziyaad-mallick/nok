import sys
import time

from miko import tts


def test_build_ps_command_escapes_single_quotes():
    cmd = tts._build_ps_command("it's a test")
    script = cmd[-1]
    assert "it''s a test" in script  # doubled single quote
    assert cmd[0] == "powershell"


def test_say_launches_and_gates(monkeypatch):
    events = []
    # Replace the real speak with a trivially fast fake command.
    fake_cmd = [sys.executable, "-c", "pass"]
    result = tts.say(
        "hello",
        on_start=lambda: events.append("start"),
        on_end=lambda: events.append("end"),
        cooldown=0,
        _command=fake_cmd,
    )
    assert result == "spoke"
    assert events == ["start", "end"]  # gate opened then closed


def test_say_times_out_and_kills_hanging_command():
    # A command that would hang for 30s; the 1s timeout must kill it.
    hang = [sys.executable, "-c", "import time; time.sleep(30)"]
    t0 = time.time()
    result = tts.say("x", cooldown=0, timeout=1, _command=hang)
    elapsed = time.time() - t0
    assert result == "tts timed out"
    assert elapsed < 10  # proves it did not run the full 30s


def test_gate_closes_even_on_timeout():
    events = []
    hang = [sys.executable, "-c", "import time; time.sleep(30)"]
    tts.say("x", on_start=lambda: events.append("s"),
            on_end=lambda: events.append("e"),
            cooldown=0, timeout=1, _command=hang)
    assert events == ["s", "e"]  # gate released despite the kill
