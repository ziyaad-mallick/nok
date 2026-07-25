"""v2 overlay behaviour: the WOKE spark, SPEAKING, and audio-reactive rings.

All headless — `now` is injected, tkinter is never imported.
"""
import pytest

from nok.overlay import (
    RING_MIN_GAP,
    RING_ONSET,
    WOKE_HOLD,
    LevelSmoother,
    OverlayModel,
)

T0 = 100.0


def _model_in(state, detail="", now=T0):
    m = OverlayModel()
    m.apply(state, detail, now)
    return m


# -- LevelSmoother --------------------------------------------------------

def test_level_starts_silent():
    assert LevelSmoother().level == 0.0


def test_level_ignores_noise_below_the_floor():
    s = LevelSmoother()
    s.push(0.0, T0)
    for i in range(20):
        s.push(0.005, T0 + i * 0.03)   # under NOISE_FLOOR
    assert s.level == pytest.approx(0.0, abs=1e-6)


def test_level_rises_on_speech():
    s = LevelSmoother()
    s.push(0.0, T0)
    for i in range(1, 20):
        s.push(0.25, T0 + i * 0.03)
    assert s.level > 0.5


def test_attack_is_faster_than_decay():
    """Rise should feel instant; fall should not strobe between syllables."""
    up = LevelSmoother()
    up.push(0.0, T0)
    up.push(0.30, T0 + 0.05)
    risen = up.level

    down = LevelSmoother()
    down.push(0.0, T0)
    for i in range(1, 40):
        down.push(0.30, T0 + i * 0.03)
    peak = down.level
    down.push(0.0, T0 + 40 * 0.03 + 0.05)
    fallen = peak - down.level

    assert risen > fallen


def test_level_is_bounded():
    s = LevelSmoother()
    s.push(0.0, T0)
    for i in range(1, 100):
        s.push(50.0, T0 + i * 0.03)   # absurdly loud
    assert s.level <= 1.0


def test_decay_to_drives_silence_without_new_audio():
    s = LevelSmoother()
    s.push(0.0, T0)
    for i in range(1, 30):
        s.push(0.3, T0 + i * 0.03)
    loud = s.level
    for i in range(30, 70):
        s.decay_to(T0 + i * 0.03)
    assert s.level < loud / 2


# -- WOKE -----------------------------------------------------------------

def test_woke_shows_no_text():
    """The spark says "I heard you" — adding a label would slow it down."""
    v = _model_in("WOKE", "summoned").view(T0)
    assert v["mode"] == "WOKE"
    assert v["title"] == ""


def test_woke_accent_is_not_a_hue():
    assert _model_in("WOKE").view(T0)["accent"] == "#F4EEE4"


def test_woke_overshoots_then_settles():
    m = _model_in("WOKE")
    start = m.view(T0 + 0.001)["orb"]["scale"]
    mid = m.view(T0 + WOKE_HOLD * 0.5)["orb"]["scale"]
    assert start < 1.0, "should start small"
    assert mid > 1.0, "should overshoot past full size"


def test_woke_auto_resolves_into_listening():
    """The caller never has to schedule the follow-up."""
    m = _model_in("WOKE")
    assert m.view(T0 + WOKE_HOLD * 0.5)["mode"] == "WOKE"
    assert m.view(T0 + WOKE_HOLD + 0.01)["mode"] == "LISTENING"


# -- SPEAKING -------------------------------------------------------------

def test_speaking_has_its_own_accent():
    assert _model_in("SPEAKING").view(T0)["accent"] == "#E8A83D"


def test_speaking_orb_tracks_output_audio_not_the_clock():
    m = _model_in("SPEAKING")
    quiet = m.view(T0)["orb"]["scale"]
    m.push_level("out", 0.0, T0)
    for i in range(1, 20):
        m.push_level("out", 0.3, T0 + i * 0.03)
    loud = m.view(T0 + 0.6)["orb"]["scale"]
    assert loud > quiet


def test_mic_and_output_levels_do_not_contaminate_each_other():
    m = _model_in("LISTENING")
    m.push_level("out", 0.0, T0)
    for i in range(1, 20):
        m.push_level("out", 0.4, T0 + i * 0.03)
    assert m.level_for("LISTENING") == pytest.approx(0.0, abs=1e-6)
    assert m.level_for("SPEAKING") > 0.3


# -- rings ----------------------------------------------------------------

def _speak_burst(m, source, now, n=12, rms=0.3):
    m.push_level(source, 0.0, now)
    for i in range(1, n):
        m.push_level(source, rms, now + i * 0.03)
    return now + n * 0.03


def test_silence_produces_no_rings():
    m = _model_in("LISTENING")
    m.push_level("mic", 0.0, T0)
    for i in range(1, 30):
        m.push_level("mic", 0.001, T0 + i * 0.03)
    assert m.view(T0 + 1.0)["rings"] == []


def test_speech_onset_spawns_a_ring():
    m = _model_in("LISTENING")
    end = _speak_burst(m, "mic", T0)
    assert len(m.view(end)["rings"]) >= 1


def test_rings_do_not_spam_on_continuous_speech():
    m = _model_in("LISTENING")
    m.push_level("mic", 0.0, T0)
    # oscillate across the onset threshold as fast as possible
    for i in range(1, 200):
        m.push_level("mic", 0.4 if i % 2 else 0.0, T0 + i * 0.01)
    spawns = m._ring_spawns
    gaps = [b - a for a, b in zip(spawns, spawns[1:])]
    assert all(g >= RING_MIN_GAP - 1e-9 for g in gaps)


def test_listening_rings_travel_inward_and_speaking_outward():
    """The direction flip is what separates 'partner' from 'VU meter'."""
    lm = _model_in("LISTENING")
    end = _speak_burst(lm, "mic", T0)
    lr = lm.view(end)["rings"]

    sm = _model_in("SPEAKING")
    end2 = _speak_burst(sm, "out", T0)
    sr = sm.view(end2)["rings"]

    assert lr and sr
    assert lr[0]["direction"] == "in"
    assert sr[0]["direction"] == "out"


def test_ring_radius_actually_moves_in_the_stated_direction():
    m = _model_in("LISTENING")
    _speak_burst(m, "mic", T0)
    spawn = m._ring_spawns[0]          # sample relative to the ring's own birth
    early = m.view(spawn + 0.02)["rings"]
    late = m.view(spawn + 0.30)["rings"]
    assert early and late
    assert late[0]["radius"] < early[0]["radius"], "inward ring should shrink"


def test_speaking_ring_radius_grows():
    m = _model_in("SPEAKING")
    _speak_burst(m, "out", T0)
    spawn = m._ring_spawns[0]
    early = m.view(spawn + 0.02)["rings"]
    late = m.view(spawn + 0.30)["rings"]
    assert early and late
    assert late[0]["radius"] > early[0]["radius"], "outward ring should grow"


def test_rings_expire():
    m = _model_in("LISTENING")
    end = _speak_burst(m, "mic", T0)
    assert m.view(end)["rings"]
    assert m.view(end + 5.0)["rings"] == []


def test_no_rings_outside_conversational_states():
    for mode in ("THINKING", "ACTING", "ERROR", "SLEEPING"):
        m = _model_in(mode)
        _speak_burst(m, "mic", T0)
        assert m.view(T0 + 0.4)["rings"] == [], f"{mode} should not ring"


# -- unknown input --------------------------------------------------------

def test_unknown_state_still_renders_and_never_raises():
    v = _model_in("BANANA", "???").view(T0)
    assert v["mode"] == "NEUTRAL"
    assert v["accent"]


def test_push_level_with_an_unknown_source_does_not_raise():
    m = _model_in("LISTENING")
    m.push_level("nonsense", 0.2, T0)  # falls back to the output smoother
