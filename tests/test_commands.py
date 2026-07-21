"""Spoken phrase -> intent.

The bug these exist for: the user said "open Spotify" five times and nothing
happened, because the agent was built to converse rather than to act. Every
phrasing below is one a person actually uses, plus the specific ways Whisper
mangles short commands.
"""
import pytest

from miko import commands
from miko.commands import (
    MEDIA,
    OPEN_APP,
    SCREENSHOT,
    TYPE_TEXT,
    UNKNOWN,
    normalize,
    parse,
)


def intent(text):
    return parse(text)[0]


def args(text):
    return parse(text)[1]


# -- open ------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "open spotify",
    "Open Spotify.",
    "Can you please open Spotify?",
    "launch spotify",
    "start spotify",
    "fire up spotify",
    "uh, open spotify for me",
    "Open the Spotify app.",
    "hey miko open spotify",
    "just open spotify please",
])
def test_every_way_a_person_says_open_spotify(said):
    assert parse(said) == (OPEN_APP, {"name": "spotify"})


def test_open_keeps_multiword_names():
    assert args("open visual studio code")["name"] == "visual studio code"


def test_open_with_nothing_to_open_is_unknown():
    assert intent("open") == UNKNOWN


# -- screenshot ------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "take a screenshot", "screenshot", "screenshot.", "Screen shot please.",
    "grab the screen", "capture the screen", "screencap",
])
def test_screenshot_phrasings(said):
    assert intent(said) == SCREENSHOT


# -- media -----------------------------------------------------------------

@pytest.mark.parametrize("said,cmd", [
    ("pause", "play"), ("play", "play"), ("play the music", "play"),
    ("resume", "play"), ("stop", "play"),
    ("next", "next"), ("next track", "next"), ("skip this song", "next"),
    ("previous", "previous"), ("go back a track", "previous"),
    ("mute", "mute"), ("silence", "mute"), ("quiet", "mute"),
    ("volume up", "volume_up"), ("louder", "volume_up"), ("turn it up", "volume_up"),
    ("volume down", "volume_down"), ("quieter", "volume_down"),
])
def test_media_phrasings(said, cmd):
    got_intent, got_args = parse(said)
    assert got_intent == MEDIA
    assert got_args["command"] == cmd


@pytest.mark.parametrize("heard,meant", [
    ("moot", "mute"), ("mut", "mute"), ("necks", "next"), ("nest", "next"),
])
def test_whisper_homophones_on_short_commands(heard, meant):
    """One-syllable commands are exactly what an STT model gets wrong."""
    assert args(heard)["command"] == meant


@pytest.mark.parametrize("said,n", [
    ("volume up", 1), ("volume up two", 2), ("volume up three", 3),
    ("volume up 5", 5), ("next twice", 2),
])
def test_repeat_counts(said, n):
    assert args(said)["repeat"] == n


def test_repeat_is_clamped():
    assert args("volume up 900")["repeat"] <= 10


# -- dictation -------------------------------------------------------------

def test_type_captures_the_body():
    assert parse("type hello world") == (TYPE_TEXT, {"text": "hello world"})


def test_type_preserves_original_casing():
    assert args("type Hello World")["text"] == "Hello World"


def test_write_this_colon_form():
    assert args("write this: the meeting is at four")["text"] == "the meeting is at four"


def test_an_explicit_type_verb_beats_a_command_keyword_inside_it():
    """"type screenshot" must type the word, not fire the screenshot action."""
    assert parse("type screenshot") == (TYPE_TEXT, {"text": "screenshot"})


def test_type_with_no_body_is_unknown():
    assert intent("type") == UNKNOWN


# -- refusing to guess -----------------------------------------------------

@pytest.mark.parametrize("said", [
    "what is the weather", "tell me a joke", "how are you",
    "", "   ", None, "asdfghjkl",
])
def test_unrecognised_input_is_reported_not_guessed(said):
    """Never fall through to typing an unknown sentence into the focused
    window: that is how a stray phrase gets executed in a terminal."""
    assert intent(said) == UNKNOWN


def test_unknown_carries_no_args():
    assert parse("something nonsensical") == (UNKNOWN, {})


# -- normalize -------------------------------------------------------------

def test_normalize_strips_punctuation_and_filler():
    assert normalize("Um, could you please take a screenshot?") == "take a screenshot"


def test_normalize_is_idempotent():
    once = normalize("Please, open Spotify!")
    assert normalize(once) == once


def test_normalize_handles_none():
    assert normalize(None) == ""


# -- describe --------------------------------------------------------------

def test_describe_covers_every_intent():
    for i, a in [(SCREENSHOT, {}), (MEDIA, {"command": "play", "repeat": 1}),
                 (OPEN_APP, {"name": "spotify"}), (TYPE_TEXT, {"text": "hi"}),
                 (UNKNOWN, {})]:
        assert commands.describe(i, a).strip()


def test_describe_mentions_repeat_count():
    assert "3" in commands.describe(MEDIA, {"command": "volume_up", "repeat": 3})
