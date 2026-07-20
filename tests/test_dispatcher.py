import pytest

from miko import actions as actions_mod
from miko.actions import Actions
from miko.dispatcher import Dispatcher

APPS = {
    "spotify": {"launch": "spotify:", "process": "Spotify.exe"},
    "notepad": {"launch": "notepad.exe", "process": "notepad.exe"},
}
MACROS = {
    "focus": [
        {"action": "media", "args": {"command": "mute"}},
        {"action": "open_app", "args": {"name": "notepad"}},
    ]
}


@pytest.fixture
def seams(monkeypatch):
    """Patch every OS-boundary seam and record calls."""
    calls = {"start": [], "run": [], "type": [], "vk": [], "shot": []}
    monkeypatch.setattr(actions_mod, "_startfile", lambda t: calls["start"].append(t))
    monkeypatch.setattr(actions_mod, "_run", lambda c: calls["run"].append(c))
    monkeypatch.setattr(actions_mod, "_type", lambda t: calls["type"].append(t))
    monkeypatch.setattr(actions_mod, "_tap_vk", lambda vk: calls["vk"].append(vk))
    monkeypatch.setattr(actions_mod, "_grab_screenshot", lambda d: calls["shot"].append(d))
    return calls


@pytest.fixture
def disp():
    return Dispatcher(Actions(APPS, MACROS))


def test_open_app(disp, seams):
    result = disp.dispatch({"action": "open_app", "args": {"name": "spotify"}})
    assert seams["start"] == ["spotify:"]
    assert "spotify" in result.lower()


def test_none_action_does_nothing_and_types_nothing(disp, seams):
    # Regression: the mock no-match default used to be type_text, which typed
    # "sorry, I didn't catch that" into the focused window. `none` must touch
    # no OS seam and return "" so the caller just speaks the 'say'.
    result = disp.dispatch({"action": "none", "args": {}, "say": "Didn't catch that."})
    assert result == ""
    assert seams["type"] == [] and seams["start"] == [] and seams["vk"] == []


def test_open_unknown_app_returns_error_no_launch(disp, seams):
    result = disp.dispatch({"action": "open_app", "args": {"name": "ghost"}})
    assert seams["start"] == []
    assert "ghost" in result


def test_close_app(disp, seams):
    disp.dispatch({"action": "close_app", "args": {"name": "notepad"}})
    assert seams["run"] == [["taskkill", "/IM", "notepad.exe", "/F"]]


def test_type_text(disp, seams):
    disp.dispatch({"action": "type_text", "args": {"text": "hello world"}})
    assert seams["type"] == ["hello world"]


def test_media_mute_taps_mute_vk(disp, seams):
    disp.dispatch({"action": "media", "args": {"command": "mute"}})
    assert seams["vk"] == [actions_mod.VK_VOLUME_MUTE]


def test_media_unknown_command(disp, seams):
    result = disp.dispatch({"action": "media", "args": {"command": "explode"}})
    assert seams["vk"] == []
    assert "explode" in result


def test_screenshot(disp, seams):
    result = disp.dispatch({"action": "screenshot", "args": {}})
    assert len(seams["shot"]) == 1
    assert "screenshot" in result.lower()


def test_macro_runs_each_step(disp, seams):
    result = disp.dispatch({"action": "macro", "args": {"name": "focus"}})
    assert seams["vk"] == [actions_mod.VK_VOLUME_MUTE]  # step 1: media mute
    assert seams["start"] == ["notepad.exe"]            # step 2: open notepad
    assert "focus" in result


def test_unknown_macro_name_returns_error(disp, seams):
    result = disp.dispatch({"action": "macro", "args": {"name": "nope"}})
    assert "nope" in result


def test_unknown_action_rejected_defense_in_depth(disp, seams):
    result = disp.dispatch({"action": "rm_rf", "args": {}})
    assert "not an allowed action" in result


def test_unknown_skill_without_runner_returns_error(disp, seams):
    result = disp.dispatch({"action": "skill", "args": {"name": "dance"}})
    assert "dance" in result


def test_skill_runner_invoked_when_present(seams):
    d = Dispatcher(Actions(APPS, MACROS), skill_runner=lambda name: f"ran {name}")
    assert d.dispatch({"action": "skill", "args": {"name": "dance"}}) == "ran dance"


def test_action_exception_returns_error_string_not_raise(disp, seams, monkeypatch):
    def boom(t):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(actions_mod, "_type", boom)
    result = disp.dispatch({"action": "type_text", "args": {"text": "hi"}})
    assert "went wrong" in result.lower()


def test_busy_gate_drops_reentrant_dispatch(seams):
    # A slow action that re-dispatches from inside proves the BUSY drop.
    outer = Dispatcher(Actions(APPS, MACROS))
    results = {}

    def slow_type(text):
        # while we're "busy", a second public dispatch must be dropped
        results["inner"] = outer.dispatch({"action": "media", "args": {"command": "mute"}})
    import miko.actions as am
    seams  # keep fixture active
    am._type = slow_type
    results["outer"] = outer.dispatch({"action": "type_text", "args": {"text": "hi"}})
    assert results["inner"] == "Still working on the last thing."
