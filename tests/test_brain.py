import sys

import pytest

from miko.brain import (
    BrainTimeout,
    CodexBrain,
    MockBrain,
    QuotaExhausted,
    build_prompt,
    wants_screenshot,
)
from miko.jsonx import ContractError


# -- MockBrain: contract + misbehavior modes ------------------------------

def test_mock_normal_returns_validated_action():
    brain = MockBrain()
    action = brain.decide("please open spotify")
    assert action == {"action": "open_app", "args": {"name": "spotify"}, "say": "Opening Spotify."}


def test_mock_default_when_no_rule_matches():
    brain = MockBrain()
    action = brain.decide("blah blah nonsense")
    # No match must be a harmless "none" (do nothing + say so), NOT a real
    # action -- the old default typed "sorry, I didn't catch that" into the
    # focused window, a genuine bug.
    assert action["action"] == "none"
    assert action["args"] == {}
    assert "catch" in action["say"].lower()


def test_mock_prose_mode_is_extracted_and_validated():
    brain = MockBrain()
    action = brain.decide("__prose__ pause the music")
    assert action == {"action": "media", "args": {"command": "pause"}, "say": "Pausing."}


def test_mock_no_json_mode_raises_contract_error():
    brain = MockBrain()
    with pytest.raises(ContractError):
        brain.decide("__nojson__ do something")


def test_mock_timeout_mode_raises_brain_timeout():
    brain = MockBrain()
    with pytest.raises(BrainTimeout):
        brain.decide("__timeout__")


def test_mock_quota_mode_raises_quota_exhausted():
    brain = MockBrain()
    with pytest.raises(QuotaExhausted):
        brain.decide("__quota__")


# -- prompt builder / screenshot gating -----------------------------------

def test_prompt_includes_toolbox_and_transcript():
    prompt = build_prompt("open spotify", {"skill_manifest": "focusmode: focus, deep"})
    assert "open_app" in prompt
    assert "open spotify" in prompt
    assert "focusmode" in prompt


@pytest.mark.parametrize("transcript,expected", [
    ("what am i looking at", True),
    ("what's this on my screen", True),
    ("open spotify", False),
    ("mute the music", False),
])
def test_wants_screenshot_triggers(transcript, expected):
    assert wants_screenshot(transcript) is expected


def test_wants_screenshot_force_flag():
    assert wants_screenshot("open spotify", force=True) is True


# -- CodexBrain: FAKE subprocesses only (never real codex) ----------------

class _TimeoutCodexBrain(CodexBrain):
    TIMEOUT_INTENT = 1  # keep the test fast

    def _build_command(self, prompt, output_file, image):
        # A real child process that hangs, to prove the timeout-kill path.
        return [sys.executable, "-c", "import time; time.sleep(30)"]


def test_codex_timeout_kills_and_raises():
    with pytest.raises(BrainTimeout):
        _TimeoutCodexBrain().decide("open spotify")


class _QuotaCodexBrain(CodexBrain):
    def _build_command(self, prompt, output_file, image):
        return [sys.executable, "-c",
                "import sys; sys.stderr.write('You have hit your usage limit (429)')"]


def test_codex_quota_detected_from_stderr():
    with pytest.raises(QuotaExhausted):
        _QuotaCodexBrain().decide("open spotify")


class _HappyCodexBrain(CodexBrain):
    def _build_command(self, prompt, output_file, image):
        # Write a valid contract JSON to the -o file (Amendment 2 primary channel).
        payload = '{"action": "media", "args": {"command": "mute"}, "say": "Muting."}'
        code = (
            "import sys; open(sys.argv[1], 'w', encoding='utf-8')"
            f".write({payload!r})"
        )
        return [sys.executable, "-c", code, output_file]


def test_codex_parses_output_file():
    action = _HappyCodexBrain().decide("mute please")
    assert action == {"action": "media", "args": {"command": "mute"}, "say": "Muting."}
