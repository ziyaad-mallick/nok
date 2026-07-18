from miko.matcher import Matcher, build_default_matcher

APPS = {"spotify": {"launch": "spotify:"}, "notepad": {"launch": "notepad.exe"}}


def test_match_media_mute():
    m = build_default_matcher(APPS)
    action = m.match("hey miko please mute the music")
    assert action == {"action": "media", "args": {"command": "mute"}, "say": "Muting."}


def test_match_open_app_beats_generic_verb():
    m = build_default_matcher(APPS)
    action = m.match("open spotify now")
    assert action == {"action": "open_app", "args": {"name": "spotify"},
                      "say": "Opening spotify."}


def test_match_no_keywords_returns_none():
    m = build_default_matcher(APPS)
    assert m.match("the weather is lovely today") is None


def test_match_empty_transcript_returns_none():
    m = build_default_matcher(APPS)
    assert m.match("") is None


def test_match_returns_copy_not_shared_reference():
    m = build_default_matcher(APPS)
    a1 = m.match("mute")
    a1["args"]["command"] = "TAMPERED"
    a2 = m.match("mute")
    assert a2["args"]["command"] == "mute"


def test_register_skill_keywords_routing():
    m = Matcher()
    m.register("focusmode", ["focus", "deep", "work"],
               {"action": "skill", "args": {"name": "focusmode"}})
    action = m.match("enter deep work focus")
    assert action == {"action": "skill", "args": {"name": "focusmode"}}
