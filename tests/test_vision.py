"""`--vision`: payload shape, answer extraction, and the no-network failure path.

The HTTP call is the one thing not tested — it is the OpenAI SDK's job. What is
tested is everything either side of it, because those are the parts that fail
quietly: a malformed content list means the model never sees the screen, and an
unhandled empty choice takes the session down instead of saying "no answer".
"""
import asyncio
import types

import pytest

from nok import vision
from nok.vision import VisionSession, build_messages, extract_answer, model_name


def _response(content):
    """The minimum shape of a chat completion, as the SDK returns it."""
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


# -- payload --------------------------------------------------------------

def test_the_image_is_actually_in_the_payload():
    msgs = build_messages("data:image/jpeg;base64,AAAA")
    parts = msgs[1]["content"]
    image = [p for p in parts if p["type"] == "image_url"]
    assert len(image) == 1
    assert image[0]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"


def test_the_system_prompt_caps_the_answer_length():
    """The answer lands in a balloon; an unbounded prompt produces an essay."""
    system = build_messages("data:x")[0]
    assert system["role"] == "system"
    assert "40 words" in system["content"]


def test_a_blank_question_still_asks_something():
    text = build_messages("data:x", "   ")[1]["content"][0]["text"]
    assert text.strip()


def test_a_real_question_is_passed_through():
    text = build_messages("data:x", "what is failing?")[1]["content"][0]["text"]
    assert text == "what is failing?"


# -- extraction -----------------------------------------------------------

def test_plain_string_content():
    assert extract_answer(_response("  the test asserts 3, got 4  ")) == \
        "the test asserts 3, got 4"


def test_content_parts_are_joined():
    parts = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert extract_answer(_response(parts)) == "ab"


@pytest.mark.parametrize("bad", [None, types.SimpleNamespace(choices=[]), object()])
def test_a_missing_answer_is_empty_not_an_exception(bad):
    assert extract_answer(bad) == ""


def test_none_content_is_empty():
    assert extract_answer(_response(None)) == ""


# -- model selection ------------------------------------------------------

def test_the_default_model_can_see():
    assert model_name() == vision.DEFAULT_MODEL


def test_the_model_is_overridable(monkeypatch):
    monkeypatch.setenv("NOK_VISION_MODEL_OPENAI", "some-other-model")
    assert model_name() == "some-other-model"


def test_a_blank_override_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("NOK_VISION_MODEL_OPENAI", "   ")
    assert model_name() == vision.DEFAULT_MODEL


# -- the session ----------------------------------------------------------

def test_the_session_reports_no_levels_because_it_has_no_microphone():
    assert VisionSession(api_key="k").levels() == (0.0, 0.0)


def test_a_failing_call_notifies_instead_of_raising(monkeypatch):
    """A dead network must reach the user as a notification, not a traceback
    out of the asyncio loop."""
    monkeypatch.setattr(vision.VisionSession, "_ask",
                        lambda self, image, q: (_ for _ in ()).throw(RuntimeError("no route")))
    monkeypatch.setattr("nok.screen.data_url", lambda *a, **k: "data:image/jpeg;base64,AAAA")

    seen = []
    session = VisionSession(api_key="k", on_answer=lambda t, b="": seen.append((t, b)))
    stats = asyncio.run(session.run())

    assert stats.errors
    assert seen and "no route" in seen[0][1]


def test_a_successful_look_answers_once(monkeypatch):
    monkeypatch.setattr(vision.VisionSession, "_ask", lambda self, image, q: "port 5173 is busy")
    monkeypatch.setattr("nok.screen.data_url", lambda *a, **k: "data:image/jpeg;base64,AAAA")

    seen = []
    session = VisionSession(api_key="k", on_answer=lambda t, b="": seen.append((t, b)))
    stats = asyncio.run(session.run())

    assert stats.turns == 1
    assert stats.answer == "port 5173 is busy"
    assert seen == [("NOK", "port 5173 is busy")]


def test_an_empty_answer_still_tells_the_user_something(monkeypatch):
    monkeypatch.setattr(vision.VisionSession, "_ask", lambda self, image, q: "")
    monkeypatch.setattr("nok.screen.data_url", lambda *a, **k: "data:image/jpeg;base64,AAAA")

    seen = []
    session = VisionSession(api_key="k", on_answer=lambda t, b="": seen.append((t, b)))
    asyncio.run(session.run())

    assert seen and seen[0][1]
