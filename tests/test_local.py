"""Local agent: model selection, reasoning-block stripping, session shape.

No Ollama, no microphone, no speakers. The parts that need those are exercised
by hand; everything here is pure logic that can silently rot.
"""
import numpy as np
import pytest

from miko import local
from miko.local import LocalSession, pick_model, strip_thinking


# -- strip_thinking -------------------------------------------------------
# A reasoning model spends its token budget inside <think> before answering.
# Unstripped, the user hears the model's private reasoning read aloud;
# unbudgeted, they hear nothing at all. Both happened on this machine.

def test_removes_a_closed_thinking_block():
    assert strip_thinking("<think>hmm</think>The answer is 4.") == "The answer is 4."


def test_removes_a_multiline_thinking_block():
    got = strip_thinking("<think>\nstep one\nstep two\n</think>\nDone.")
    assert got == "Done."


def test_drops_everything_after_an_unclosed_opener():
    """Budget ran out mid-thought: speak nothing rather than raw reasoning."""
    assert strip_thinking("<think>I should consider") == ""


def test_keeps_the_answer_when_the_opener_comes_after_it():
    assert strip_thinking("Answer first.<think>then musing") == "Answer first."


def test_handles_several_blocks():
    assert strip_thinking("<think>a</think>X<think>b</think>Y") == "XY"


def test_is_case_insensitive():
    assert strip_thinking("<THINK>a</THINK>Hello") == "Hello"


def test_plain_text_is_untouched():
    assert strip_thinking("Just an answer.") == "Just an answer."


@pytest.mark.parametrize("junk", [None, "", "   "])
def test_empty_inputs_are_safe(junk):
    assert strip_thinking(junk) == ""


# -- pick_model -----------------------------------------------------------

def _fake_tags(monkeypatch, names):
    monkeypatch.setattr(local, "_ollama_models", lambda: list(names))


def test_prefers_the_configured_vision_model(monkeypatch):
    _fake_tags(monkeypatch, ["qwen3:latest", "moondream:latest"])
    monkeypatch.setattr(local, "VISION_MODEL", "moondream")
    model, vision = pick_model()
    assert model == "moondream:latest" and vision is True


def test_falls_back_to_any_known_vision_model(monkeypatch):
    _fake_tags(monkeypatch, ["qwen3:latest", "llava:7b"])
    monkeypatch.setattr(local, "VISION_MODEL", "moondream")
    model, vision = pick_model()
    assert model == "llava:7b" and vision is True


def test_falls_back_to_a_text_model_and_says_so(monkeypatch):
    """Text-only must report vision=False so the session skips the screenshot."""
    _fake_tags(monkeypatch, ["goekdenizguelmez/JOSIEFIED-Qwen3:latest"])
    monkeypatch.setattr(local, "VISION_MODEL", "moondream")
    model, vision = pick_model()
    assert model == "goekdenizguelmez/JOSIEFIED-Qwen3:latest"
    assert vision is False


def test_no_models_installed(monkeypatch):
    _fake_tags(monkeypatch, [])
    assert pick_model() == (None, False)


def test_ollama_unreachable_is_not_an_exception(monkeypatch):
    def boom():
        raise ConnectionError("refused")
    monkeypatch.setattr(local, "_ollama_models", boom)
    assert pick_model() == (None, False)


def test_explicit_text_model_override_wins(monkeypatch):
    _fake_tags(monkeypatch, ["moondream:latest", "phi4:latest"])
    monkeypatch.setattr(local, "TEXT_MODEL", "phi4")
    model, vision = pick_model()
    assert model == "phi4:latest" and vision is False


# -- session shape --------------------------------------------------------

def test_exposes_the_same_interface_as_the_realtime_session():
    """app.Miko must not be able to tell the two backends apart."""
    from miko.realtime import RealtimeSession
    for name in ("run", "levels", "request_stop", "interrupt"):
        assert hasattr(LocalSession(), name), f"LocalSession is missing {name}"
        assert hasattr(RealtimeSession(api_key="x"), name)


def test_levels_starts_silent():
    assert LocalSession().levels() == (0.0, 0.0)


def test_request_stop_is_immediate_and_idempotent():
    s = LocalSession()
    s.request_stop()
    s.request_stop()
    assert s._stop is True


async def test_run_reports_an_error_when_no_model_is_available(monkeypatch):
    seen = []
    monkeypatch.setattr(local, "pick_model", lambda: (None, False))
    s = LocalSession(on_state=lambda st, d="": seen.append(st))
    stats = await s.run()
    assert "ERROR" in seen
    assert stats.errors


async def test_a_stopped_session_does_no_turns(monkeypatch):
    monkeypatch.setattr(local, "pick_model", lambda: ("fake", False))
    s = LocalSession()
    s.request_stop()
    stats = await s.run()
    assert stats.turns == 0


# -- recorder gating ------------------------------------------------------

def test_recorder_starts_silent():
    assert local.Recorder().level == 0.0


def test_endpoint_constants_are_sane():
    """Guards against a careless edit making the agent uninterruptible."""
    assert local.MIN_SPEECH_S < local.MAX_UTTERANCE_S
    assert local.SILENCE_HANG_S < local.MAX_UTTERANCE_S
    assert local.NOISE_MULT > 1.0
