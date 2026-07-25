"""The notification layer's pure half.

`Tray` itself talks to Win32 and needs a real desktop, so it is exercised by
hand. What is tested here is everything that decides *what the user reads*:
truncation against the balloon's hard field limits, and the mapping from an
intent to a message. Those are the parts that can be silently wrong.
"""
import pytest

from nok.notify import TEXT_MAX, TITLE_MAX, action_message, fit


# -- fit ------------------------------------------------------------------

def test_short_text_is_untouched():
    assert fit("Screenshot saved", 63) == "Screenshot saved"


def test_long_text_is_cut_to_the_limit():
    assert len(fit("x" * 500, 63)) == 63


def test_a_cut_string_ends_in_one_ellipsis():
    got = fit("x" * 500, 20)
    assert got.endswith("…")
    assert got.count("…") == 1


def test_whitespace_is_collapsed_so_paths_and_answers_stay_one_line():
    assert fit("a\n\n  b\tc", 63) == "a b c"


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_is_empty_output(value):
    assert fit(value, 63) == ""


def test_a_string_exactly_at_the_limit_is_not_cut():
    assert fit("x" * 63, 63) == "x" * 63


# -- action_message -------------------------------------------------------

def test_screenshot_says_where_the_file_went():
    title, body = action_message("screenshot", r"C:\Users\me\Pictures\nok\nok-1.png")
    assert title == "Screenshot saved"
    assert "nok-1.png" in body


def test_play_pause_needs_no_body():
    title, body = action_message("media_play_pause", "play/pause")
    assert title == "Play / pause"
    assert body == ""


def test_opening_an_app_puts_the_result_in_the_title():
    title, body = action_message("open_fav_app", "Opening spotify.")
    assert "spotify" in title
    assert body == ""


def test_an_unknown_intent_still_produces_something_readable():
    title, body = action_message("some_new_thing", "it happened")
    assert title == "Some New Thing"
    assert body == "it happened"


def test_messages_never_exceed_the_win32_field_limits():
    title, body = action_message("screenshot", "C:/" + "deep/" * 200 + "shot.png")
    assert len(title) <= TITLE_MAX
    assert len(body) <= TEXT_MAX
