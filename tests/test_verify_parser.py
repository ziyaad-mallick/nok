import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.verify_codex import extract_last_json_block


def test_clean_json():
    text = '{"action": "open_app", "args": {"name": "spotify"}}'
    assert extract_last_json_block(text) == {
        "action": "open_app",
        "args": {"name": "spotify"},
    }


def test_json_wrapped_in_prose():
    text = (
        "Sure, here you go:\n"
        '{"action": "open_app", "say": "opening spotify"}\n'
        "Let me know if you need anything else."
    )
    assert extract_last_json_block(text) == {
        "action": "open_app",
        "say": "opening spotify",
    }


def test_multiple_json_blocks_takes_last():
    text = 'first {"a": 1} then later {"b": 2}'
    assert extract_last_json_block(text) == {"b": 2}


def test_no_json_returns_none():
    text = "there is no JSON in this response at all, just chatter."
    assert extract_last_json_block(text) is None


def test_truncated_json_returns_none():
    text = '{"action": "open_app", "args": {"name": "spo'
    assert extract_last_json_block(text) is None
