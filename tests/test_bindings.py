import pytest

from nok import bindings
from nok.bindings import (
    IGNORE,
    MEDIA_PLAY_PAUSE,
    OPEN_FAV_APP,
    SCREENSHOT,
    WAKE_AGENT,
    resolve,
)
from nok.ioi import DOUBLE_TAP, NONE, TRIPLE_TAP
from nok.side import LEFT, RIGHT, UNKNOWN


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


# -- --fav, the favourite-app binding -------------------------------------

def test_fav_app_takes_the_left_double_knock():
    assert resolve(DOUBLE_TAP, LEFT, fav_app="spotify").intent == OPEN_FAV_APP


def test_fav_app_never_takes_the_right_double_knock():
    """Screenshot is the headline gesture; --fav must not silently eat it."""
    assert resolve(DOUBLE_TAP, RIGHT, fav_app="spotify").intent == SCREENSHOT


def test_fav_app_wins_the_only_slot_when_sides_are_off():
    got = resolve(DOUBLE_TAP, UNKNOWN, sides_enabled=False, fav_app="spotify")
    assert got.intent == OPEN_FAV_APP


def test_without_fav_app_the_map_is_unchanged():
    assert resolve(DOUBLE_TAP, LEFT, fav_app="").intent == MEDIA_PLAY_PAUSE
    assert resolve(DOUBLE_TAP, UNKNOWN, sides_enabled=False, fav_app="").intent == SCREENSHOT


def test_fav_app_does_not_touch_the_triple_knock():
    assert resolve(TRIPLE_TAP, LEFT, fav_app="spotify").intent == WAKE_AGENT


def test_opening_an_app_is_an_instant_action():
    assert resolve(DOUBLE_TAP, LEFT, fav_app="spotify").instant is True


def test_describe_names_the_fav_app_instead_of_media():
    text = bindings.describe(True, fav_app="spotify")
    assert "open spotify" in text
    assert "play / pause" not in text


def test_describe_admits_fav_app_costs_the_screenshot_without_sides():
    text = bindings.describe(False, fav_app="spotify")
    assert "open spotify" in text
    assert "screenshot is unavailable" in text


def test_describe_labels_the_agent_it_actually_runs():
    assert "read my screen" in bindings.describe(True, agent_label="read my screen")
