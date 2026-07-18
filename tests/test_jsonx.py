import pytest

from miko.jsonx import (
    ACTION_WHITELIST,
    ContractError,
    extract_last_json_block,
    validate_action,
)


# -- extract_last_json_block (moved from verify_codex; extended) ----------

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
    assert extract_last_json_block("just chatter, no json here") is None


def test_truncated_json_returns_none():
    assert extract_last_json_block('{"action": "open_app", "args": {"name": "spo') is None


def test_empty_text_returns_none():
    assert extract_last_json_block("") is None
    assert extract_last_json_block(None) is None


# -- validate_action ------------------------------------------------------

def test_validate_good_action_normalizes_defaults():
    obj = validate_action({"action": "media"})
    assert obj["action"] == "media"
    assert obj["args"] == {}   # normalized
    assert obj["say"] == ""    # normalized


def test_validate_full_action_passes_through():
    src = {"action": "open_app", "args": {"name": "spotify"}, "say": "hi"}
    assert validate_action(src) == src


@pytest.mark.parametrize("action", ACTION_WHITELIST)
def test_validate_accepts_every_whitelisted_action(action):
    assert validate_action({"action": action})["action"] == action


def test_validate_rejects_non_dict():
    with pytest.raises(ContractError):
        validate_action([1, 2, 3])
    with pytest.raises(ContractError):
        validate_action(None)


def test_validate_rejects_unknown_action():
    with pytest.raises(ContractError):
        validate_action({"action": "rm_rf", "args": {}, "say": ""})


def test_validate_rejects_missing_action():
    with pytest.raises(ContractError):
        validate_action({"args": {}, "say": ""})


def test_validate_rejects_bad_args_type():
    with pytest.raises(ContractError):
        validate_action({"action": "media", "args": "loud"})


def test_validate_rejects_bad_say_type():
    with pytest.raises(ContractError):
        validate_action({"action": "media", "say": 5})
