import json

import pytest

from miko.config import ConfigError, load_apps, load_config, load_macros


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


# -- good configs ---------------------------------------------------------

def test_load_good_apps(tmp_path):
    p = _write(tmp_path, "apps.json", {
        "spotify": {"launch": "spotify:", "process": "Spotify.exe"},
        "browser": {"launch": "https://x.com"},  # process optional
    })
    apps = load_apps(p)
    assert apps["spotify"]["launch"] == "spotify:"


def test_load_good_macros(tmp_path):
    p = _write(tmp_path, "macros.json", {
        "focus": [
            {"action": "media", "args": {"command": "mute"}},
            {"action": "open_app", "args": {"name": "notepad"}},
        ]
    })
    macros = load_macros(p)
    assert len(macros["focus"]) == 2


def test_ships_starter_configs_validate():
    # The committed repo-root starter files must pass validation.
    apps, macros = load_config("apps.json", "macros.json")
    assert "spotify" in apps
    assert "focus" in macros


# -- broken configs -------------------------------------------------------

def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_apps(tmp_path / "nope.json")
    assert "not found" in str(e.value)


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "apps.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_apps(p)


def test_app_missing_launch_names_entry(tmp_path):
    p = _write(tmp_path, "apps.json", {"spotify": {"process": "Spotify.exe"}})
    with pytest.raises(ConfigError) as e:
        load_apps(p)
    assert "spotify" in str(e.value)
    assert "launch" in str(e.value)


def test_app_entry_not_object(tmp_path):
    p = _write(tmp_path, "apps.json", {"spotify": "spotify:"})
    with pytest.raises(ConfigError):
        load_apps(p)


def test_macro_step_not_whitelisted_names_entry(tmp_path):
    p = _write(tmp_path, "macros.json", {
        "evil": [{"action": "exec_shell", "args": {}}]
    })
    with pytest.raises(ConfigError) as e:
        load_macros(p)
    assert "evil" in str(e.value)


def test_macro_step_may_not_be_macro_recursion(tmp_path):
    p = _write(tmp_path, "macros.json", {
        "loop": [{"action": "macro", "args": {"name": "loop"}}]
    })
    with pytest.raises(ConfigError):
        load_macros(p)


def test_macro_empty_list_rejected(tmp_path):
    p = _write(tmp_path, "macros.json", {"empty": []})
    with pytest.raises(ConfigError):
        load_macros(p)
