import pytest

from miko import bindings
from miko.bindings import (
    IGNORE,
    MEDIA_PLAY_PAUSE,
    SCREENSHOT,
    WAKE_AGENT,
    resolve,
)
from miko.ioi import DOUBLE_TAP, NONE, TRIPLE_TAP
from miko.side import LEFT, RIGHT, UNKNOWN


# -- the spec, with sides working -----------------------------------------

def test_double_right_is_screenshot():
    assert resolve(DOUBLE_TAP, RIGHT).intent == SCREENSHOT


def test_double_left_is_play_pause():
    assert resolve(DOUBLE_TAP, LEFT).intent == MEDIA_PLAY_PAUSE


@pytest.mark.parametrize("side", [LEFT, RIGHT, UNKNOWN])
def test_triple_wakes_the_agent_from_any_side(side):
    assert resolve(TRIPLE_TAP, side).intent == WAKE_AGENT


# -- degraded map ---------------------------------------------------------

def test_double_falls_back_to_screenshot_when_sides_disabled():
    assert resolve(DOUBLE_TAP, LEFT, sides_enabled=False).intent == SCREENSHOT


def test_double_falls_back_to_screenshot_when_side_is_unknown():
    assert resolve(DOUBLE_TAP, UNKNOWN).intent == SCREENSHOT


def test_triple_still_wakes_the_agent_when_sides_disabled():
    assert resolve(TRIPLE_TAP, UNKNOWN, sides_enabled=False).intent == WAKE_AGENT


def test_media_is_unreachable_without_sides():
    """The fallback ladder drops media rather than rebinding it to a longer chain."""
    got = {resolve(p, s, sides_enabled=False).intent
           for p in (DOUBLE_TAP, TRIPLE_TAP, NONE)
           for s in (LEFT, RIGHT, UNKNOWN)}
    assert MEDIA_PLAY_PAUSE not in got


# -- non-gestures ---------------------------------------------------------

@pytest.mark.parametrize("pattern", [NONE, "", "QUAD_TAP"])
def test_unrecognized_patterns_are_ignored(pattern):
    assert resolve(pattern, RIGHT).intent == IGNORE


# -- instant-path guarantee ----------------------------------------------

def test_both_instant_actions_are_marked_instant():
    assert resolve(DOUBLE_TAP, RIGHT).instant is True
    assert resolve(DOUBLE_TAP, LEFT).instant is True


def test_the_agent_is_not_an_instant_action():
    assert resolve(TRIPLE_TAP, UNKNOWN).instant is False


def test_instant_intents_set_matches_the_bindings():
    for intent in bindings.INSTANT_INTENTS:
        assert bindings.BINDINGS[intent].instant is True


# -- the printed map ------------------------------------------------------

def test_describe_mentions_both_sides_when_enabled():
    text = bindings.describe(True)
    assert "right" in text and "left" in text and "play / pause" in text


def test_describe_warns_when_sides_are_off():
    text = bindings.describe(False)
    assert "side detection off" in text
    assert "play / pause" not in text
