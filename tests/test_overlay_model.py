"""Headless unit tests for OverlayModel.

Every case injects ``now`` explicitly (no wall clock, no window). tkinter is
never imported here — importing ``OverlayModel`` must stay cheap and headless.
"""
import pytest

from nok.overlay import OverlayModel

T0 = 100.0  # arbitrary monotonic origin


def _model_in(state, detail="", now=T0):
    m = OverlayModel()
    m.apply(state, detail, now)
    return m


# -- the seven modes: accent / title / expanded ----------------------------

def test_sleeping_mode_is_invisible():
    """At rest NOK has no screen presence at all — no pill, no label.

    v1 kept a persistent "NOK / knock twice" pill on screen permanently. That
    is the clutter that made the HUD read as a toy; the product should not be
    visible until it is summoned.
    """
    m = _model_in("SLEEPING", "ready — knock twice")
    v = m.view(T0)
    assert v["mode"] == "SLEEPING"
    assert v["title"] == ""
    assert v["expanded"] is False
    # fades out over ~300ms and stays gone
    assert m.view(T0 + 0.5)["window_alpha"] == pytest.approx(0.0, abs=0.01)


def test_listening_mode():
    v = _model_in("LISTENING", "woke on DOUBLE_TAP").view(T0)
    assert v["mode"] == "LISTENING"
    assert v["accent"] == "#3FD1C0"
    assert v["title"] == "Listening"
    assert v["expanded"] is False


def test_thinking_mode():
    v = _model_in("THINKING", "open spotify").view(T0)
    assert v["mode"] == "THINKING"
    assert v["accent"] == "#9B80E8"
    assert v["title"] == "Thinking"
    assert v["expanded"] is True


def test_acting_mode():
    v = _model_in("ACTING", "open_app").view(T0)
    assert v["mode"] == "ACTING"
    assert v["accent"] == "#5BC98A"
    assert v["title"] == "Acting"
    assert v["expanded"] is True


def test_teaching_mode():
    v = _model_in("TEACHING", "learn a new trick: greet").view(T0)
    assert v["mode"] == "TEACHING"
    assert v["accent"] == "#E8A83D"
    assert v["title"] == "Learning a new trick"
    assert v["expanded"] is True


def test_error_mode():
    v = _model_in("ERROR", "brain timed out").view(T0)
    assert v["mode"] == "ERROR"
    assert v["accent"] == "#E1604A"
    assert v["title"] == "Something broke"
    assert v["expanded"] is True


def test_done_mode():
    # DONE is synthetic: ACTING -> SLEEPING("cycle complete").
    m = OverlayModel()
    m.apply("ACTING", "open_app", T0)
    m.apply("SLEEPING", "cycle complete", T0 + 1.0)
    v = m.view(T0 + 1.5)
    assert v["mode"] == "DONE"
    assert v["accent"] == "#5BC98A"
    assert v["title"] == "Done"
    assert v["expanded"] is True


# -- THINKING details ------------------------------------------------------

def test_thinking_transcript_quote():
    v = _model_in("THINKING", "open spotify").view(T0)
    assert v["line2"] == '"open spotify"'
    assert v["line2_style"] == "transcript"


def test_thinking_elapsed_counter():
    v = _model_in("THINKING", "open spotify").view(T0 + 7.2)
    assert v["timer_text"] == "thinking · 0:07"


def test_thinking_verb_rotation():
    m = _model_in("THINKING", "open spotify")
    assert m.view(T0 + 0.1)["title"] == "Thinking"
    assert m.view(T0 + 4.1)["title"] == "Reading the screen"
    assert m.view(T0 + 8.1)["title"] == "Working it out"
    assert m.view(T0 + 12.1)["title"] == "Thinking"  # loops


# -- LISTENING details -----------------------------------------------------

def test_listening_sub_line():
    v = _model_in("LISTENING", "woke on DOUBLE_TAP").view(T0)
    assert "double" in v["line2"]
    v3 = _model_in("LISTENING", "woke on TRIPLE_TAP").view(T0)
    assert "triple" in v3["line2"]


def test_listening_ellipsis_dots():
    m = _model_in("LISTENING", "woke on DOUBLE_TAP")
    dots = [m.view(T0 + off)["ellipsis_dots"] for off in (0.0, 0.4, 0.8, 1.2)]
    assert dots == [0, 1, 2, 3]


# -- sticky error ----------------------------------------------------------

def test_sticky_error_holds_then_resolves():
    m = OverlayModel()
    m.apply("ERROR", "brain timed out", T0)
    m.apply("SLEEPING", "cycle complete", T0 + 2.0)
    assert m.view(T0 + 3.9)["mode"] == "ERROR"   # still sticky
    assert m.view(T0 + 4.5)["mode"] == "SLEEPING"  # released by 4.0s + fade


# -- DONE synthesis vs error -----------------------------------------------

def test_done_synthesis_then_sleeping():
    m = OverlayModel()
    m.apply("ACTING", "open_app", T0)
    m.apply("SLEEPING", "cycle complete", T0 + 1.0)
    assert m.view(T0 + 1.0 + 1.0)["mode"] == "DONE"       # within 1.8s hold
    assert m.view(T0 + 1.0 + 1.9)["mode"] == "SLEEPING"   # past the hold


def test_error_to_sleeping_is_not_done():
    m = OverlayModel()
    m.apply("ERROR", "brain timed out", T0)
    m.apply("SLEEPING", "cycle complete", T0 + 1.0)
    for off in (0.5, 1.5, 3.5, 5.0):
        assert m.view(T0 + off)["mode"] != "DONE"


def test_teaching_to_sleeping_is_done():
    m = OverlayModel()
    m.apply("TEACHING", "learn a new trick: greet", T0)
    m.apply("SLEEPING", "cycle complete", T0 + 1.0)
    assert m.view(T0 + 1.5)["mode"] == "DONE"


# -- robustness ------------------------------------------------------------

def test_unknown_state_is_neutral_card():
    v = _model_in("WAT", "???").view(T0)
    assert v["mode"] == "NEUTRAL"
    assert v["accent"] == "#8C8175"
    # a neutral card renders without raising and stays compact
    assert v["expanded"] is False


def test_repeated_same_state_does_not_restart_animation():
    m = OverlayModel()
    m.apply("THINKING", "open spotify", T0)
    entered = m.state_entered_at
    m.apply("THINKING", "open spotify", T0 + 5.0)
    assert m.state_entered_at == entered


def test_view_is_pure_for_a_fixed_now():
    m = _model_in("THINKING", "open spotify")
    now = T0 + 3.3
    assert m.view(now) == m.view(now)


def test_acting_action_chip():
    v = _model_in("ACTING", "open_app").view(T0)
    assert v["line2"] == "→ open_app"
    assert v["line2_style"] == "chip"
