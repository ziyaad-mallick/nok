"""Local agent: model selection, reasoning-block stripping, session shape.

No Ollama, no microphone, no speakers. The parts that need those are exercised
by hand; everything here is pure logic that can silently rot.
"""
import numpy as np
import pytest

from nok import local
from nok.local import LocalSession, pick_model, strip_thinking


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


def test_a_real_screen_reader_outranks_a_photo_captioner(monkeypatch):
    """moondream answered a screenshot of code with "irtf.com profile page for
    Joshua Taylor Bennett". Confident invention is worse than no vision, so a
    document/UI model must win whenever one is installed."""
    _fake_tags(monkeypatch, ["moondream:latest", "qwen2.5vl:3b"])
    monkeypatch.setattr(local, "VISION_MODEL", "")
    model, vision = pick_model()
    assert model == "qwen2.5vl:3b" and vision is True


def test_captioner_is_used_only_as_a_last_resort(monkeypatch):
    _fake_tags(monkeypatch, ["moondream:latest"])
    monkeypatch.setattr(local, "VISION_MODEL", "")
    model, vision = pick_model()
    assert model == "moondream:latest" and vision is True


def test_unreliable_list_is_a_subset_of_the_preference_list():
    assert local.UNRELIABLE_ON_SCREENS <= set(local.VISION_PREFERENCE)


def test_unreliable_models_rank_below_reliable_ones():
    ranks = {n: i for i, n in enumerate(local.VISION_PREFERENCE)}
    worst_reliable = max(r for n, r in ranks.items()
                         if n not in local.UNRELIABLE_ON_SCREENS)
    best_unreliable = min(ranks[n] for n in local.UNRELIABLE_ON_SCREENS)
    assert best_unreliable > worst_reliable


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
    """app.Nok must not be able to tell the two backends apart."""
    from nok.realtime import RealtimeSession
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
    assert local.MIN_GATE < local.MAX_GATE


# -- the speech gate ------------------------------------------------------
#
# This is the maths that decides whether NOK ever hears you. It used to be
# recalculated from scratch every turn from one short room sample, which is
# stable in a quiet room and wild in a live one -- the failure that ate the
# Build Week demo. These tests pin the behaviour that replaced it.

@pytest.fixture(autouse=True)
def _fresh_room():
    local.reset_room_noise()
    yield
    local.reset_room_noise()


def test_silent_room_still_gets_a_floor():
    """A room sampled at exactly zero is a broken mic, not a real silence."""
    assert local.next_gate(0.0) == local.MIN_GATE


def test_gate_scales_with_a_moderately_noisy_room():
    gate = local.next_gate(0.004)
    assert gate == pytest.approx(0.004 * local.NOISE_MULT)


def test_gate_is_capped_so_a_loud_room_cannot_make_it_deaf():
    """Over-triggering is recoverable. Never triggering is not."""
    assert local.next_gate(1.0) == local.MAX_GATE


def test_one_noisy_sample_does_not_swing_the_gate():
    quiet = 0.002
    for _ in range(20):
        local.next_gate(quiet)          # settle on a quiet room
    settled = local.next_gate(quiet)

    spiked = local.next_gate(0.02)      # someone slams a door

    # It must move -- but nowhere near the full distance to the spike.
    unsmoothed = min(0.02 * local.NOISE_MULT, local.MAX_GATE)
    assert spiked > settled
    assert spiked < settled + (unsmoothed - settled) * 0.5


def test_estimate_tracks_a_genuinely_changed_room():
    """Smoothing must not mean never adapting -- a real change has to land."""
    for _ in range(20):
        local.next_gate(0.001)
    for _ in range(20):
        local.next_gate(0.01)

    assert local.next_gate(0.01) == pytest.approx(0.01 * local.NOISE_MULT, rel=0.1)


def test_reset_forgets_the_room():
    for _ in range(10):
        local.next_gate(0.01)
    local.reset_room_noise()

    assert local.next_gate(0.0) == local.MIN_GATE
