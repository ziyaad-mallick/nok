from pathlib import Path

import pytest

from miko import teach
from miko.brain import MockBrain
from miko.skills_loader import SkillsLoader

_TEMPLATE = (Path(__file__).resolve().parent.parent / "skills" / "_template.py").read_text(
    encoding="utf-8"
)


@pytest.fixture
def skills_dir(tmp_path):
    (tmp_path / "_template.py").write_text(_TEMPLATE, encoding="utf-8")
    return tmp_path


def _loader(skills_dir, brain):
    return SkillsLoader(skills_dir=skills_dir, brain=brain)


# -- parsing helpers ------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("miko learn a new trick focus mode", True),
    ("can you teach you to do this", True),
    ("open spotify", False),
    ("mute the music", False),
])
def test_is_teach_intent(text, expected):
    assert teach.is_teach_intent(text) is expected


def test_parse_learn_request_strips_trigger():
    assert teach.parse_learn_request("learn a new trick: focus mode") == "focus mode"
    assert teach.parse_learn_request("teach you to mute audio") == "mute audio"


def test_slugify():
    assert teach.slugify("focus mode") == "focus_mode"
    assert teach.slugify("") == "skill"


# -- happy codegen --------------------------------------------------------

def test_happy_codegen_writes_file_at_explicit_path_and_loads(skills_dir):
    brain = MockBrain(codegen_mode="valid")
    loader = _loader(skills_dir, brain)
    say = teach.teach("learn a new trick: focus mode", brain, loader, skills_dir)

    assert (skills_dir / "focus_mode.py").exists()  # explicit-path verification
    assert "Learned 'focus_mode'" in say
    assert "focus_mode" in loader.skills
    assert brain.codegen_calls == 1  # no retry needed


# -- misbehavior branches -------------------------------------------------

def test_bad_import_branch_fails_after_retry(skills_dir):
    brain = MockBrain(codegen_mode="bad_import")
    loader = _loader(skills_dir, brain)
    say = teach.teach("learn a new trick do bad", brain, loader, skills_dir)
    assert "couldn't learn" in say.lower()
    assert brain.codegen_calls == 2  # create + one repair, then give up
    assert "do_bad" not in loader.skills


def test_broken_syntax_branch_fails_after_retry(skills_dir):
    brain = MockBrain(codegen_mode="broken_syntax")
    loader = _loader(skills_dir, brain)
    say = teach.teach("learn a new trick broken thing", brain, loader, skills_dir)
    assert "couldn't learn" in say.lower()
    assert brain.codegen_calls == 2


def test_nothing_branch_no_file_no_retry(skills_dir):
    brain = MockBrain(codegen_mode="nothing")
    loader = _loader(skills_dir, brain)
    say = teach.teach("learn a new trick empty thing", brain, loader, skills_dir)
    assert "no skill file was written" in say
    assert brain.codegen_calls == 1  # nothing to repair -> no retry
    assert not (skills_dir / "empty_thing.py").exists()


def test_retry_once_success(skills_dir):
    # First codegen writes broken syntax; the single retry writes a valid skill.
    brain = MockBrain(codegen_mode=["broken_syntax", "valid"])
    loader = _loader(skills_dir, brain)
    say = teach.teach("learn a new trick recover mode", brain, loader, skills_dir)
    assert "Learned 'recover_mode'" in say
    assert brain.codegen_calls == 2
    assert "recover_mode" in loader.skills


def test_empty_behavior_prompts_for_detail(skills_dir):
    brain = MockBrain(codegen_mode="valid")
    loader = _loader(skills_dir, brain)
    say = teach.teach("learn a new trick", brain, loader, skills_dir)
    assert "what the new trick should do" in say.lower()
    assert brain.codegen_calls == 0
