import os
from pathlib import Path

import pytest

from miko import helpers
from miko.brain import MockBrain
from miko.matcher import Matcher
from miko.skills_loader import (
    SkillLoadError,
    SkillRejected,
    SkillsLoader,
    static_check,
)

VALID = (
    "from miko import helpers\n"
    'KEYWORDS = ["alpha", "beta"]\n'
    "def run(ctx):\n"
    '    return "ran alpha: " + ctx.transcript\n'
)
VALID_V2 = (
    "from miko import helpers\n"
    'KEYWORDS = ["gamma"]\n'
    "def run(ctx):\n"
    '    return "v2"\n'
)
BAD_IMPORT = (
    "import os\n"
    "from miko import helpers\n"
    'KEYWORDS = ["x"]\n'
    "def run(ctx):\n"
    "    return os.getcwd()\n"
)
NO_KEYWORDS = (
    "from miko import helpers\n"
    "def run(ctx):\n"
    '    return "x"\n'
)
EMPTY_KEYWORDS = (
    "from miko import helpers\n"
    "KEYWORDS = []\n"
    "def run(ctx):\n"
    '    return "x"\n'
)
BROKEN_SYNTAX = (
    "from miko import helpers\n"
    'KEYWORDS = ["x"]\n'
    "def run(ctx)\n"        # missing colon
    '    return "x"\n'
)


def _write(d: Path, name: str, source: str) -> Path:
    p = d / name
    p.write_text(source, encoding="utf-8")
    return p


# -- static_check ---------------------------------------------------------

def test_static_check_allows_whitelisted_imports():
    static_check("from miko import helpers\nimport re\nimport json\n", "ok.py")
    static_check("from miko.helpers import open_app\n", "ok.py")
    static_check("import miko.helpers\n", "ok.py")


def test_static_check_rejects_disallowed_import_with_reason():
    with pytest.raises(SkillRejected) as ei:
        static_check("import os\n", "bad.py")
    assert "os" in str(ei.value)


def test_static_check_rejects_from_miko_other_than_helpers():
    with pytest.raises(SkillRejected) as ei:
        static_check("from miko import brain\n", "bad.py")
    assert "brain" in str(ei.value)


def test_static_check_rejects_relative_import():
    with pytest.raises(SkillRejected):
        static_check("from . import helpers\n", "bad.py")


def test_static_check_reports_syntax_error():
    with pytest.raises(SkillLoadError):
        static_check("def run(ctx)\n    pass\n", "bad.py")


# -- load_path / scan (no brain => no retry) ------------------------------

def test_valid_skill_loads(tmp_path):
    _write(tmp_path, "alpha.py", VALID)
    loader = SkillsLoader(skills_dir=tmp_path)
    manifest = loader.scan()
    assert manifest == [("alpha", ["alpha", "beta"])]
    assert "alpha" in loader.skills


def test_bad_import_rejected_with_reason(tmp_path):
    _write(tmp_path, "bad.py", BAD_IMPORT)
    loader = SkillsLoader(skills_dir=tmp_path)  # no brain -> no retry
    loader.scan()
    assert "bad" not in loader.skills
    assert "os" in loader.errors["bad"]


def test_keywords_missing_rejected(tmp_path):
    _write(tmp_path, "nokw.py", NO_KEYWORDS)
    loader = SkillsLoader(skills_dir=tmp_path)
    loader.scan()
    assert "nokw" not in loader.skills
    assert "KEYWORDS" in loader.errors["nokw"]


def test_empty_keywords_rejected(tmp_path):
    _write(tmp_path, "emptykw.py", EMPTY_KEYWORDS)
    loader = SkillsLoader(skills_dir=tmp_path)
    loader.scan()
    assert "emptykw" not in loader.skills


def test_template_and_dunder_files_skipped(tmp_path):
    _write(tmp_path, "_template.py", VALID)
    _write(tmp_path, "__init__.py", "")
    loader = SkillsLoader(skills_dir=tmp_path)
    assert loader.scan() == []


# -- retry path (brain present) -------------------------------------------

def test_syntax_error_retry_then_giveup(tmp_path):
    _write(tmp_path, "broken.py", BROKEN_SYNTAX)
    brain = MockBrain(codegen_mode="broken_syntax")  # rewrites broken again
    loader = SkillsLoader(skills_dir=tmp_path, brain=brain)
    loader.scan()
    assert "broken" not in loader.skills
    assert "broken" in loader.errors
    assert brain.codegen_calls == 1  # one repair attempt, then give up


def test_syntax_error_retry_then_success(tmp_path):
    _write(tmp_path, "broken.py", BROKEN_SYNTAX)
    brain = MockBrain(codegen_mode="valid")  # repair writes a valid skill
    loader = SkillsLoader(skills_dir=tmp_path, brain=brain)
    loader.scan()
    assert "broken" in loader.skills
    assert brain.codegen_calls == 1


def test_load_path_without_brain_raises(tmp_path):
    p = _write(tmp_path, "broken.py", BROKEN_SYNTAX)
    loader = SkillsLoader(skills_dir=tmp_path)
    with pytest.raises(SkillLoadError):
        loader.load_path(p)


# -- mtime hot-reload -----------------------------------------------------

def test_mtime_hotreload_skips_unchanged_reloads_changed_drops_deleted(tmp_path):
    p = _write(tmp_path, "alpha.py", VALID)
    loader = SkillsLoader(skills_dir=tmp_path)

    loader.scan()
    first = loader.skills["alpha"]

    loader.scan()  # unchanged -> not re-imported
    assert loader.skills["alpha"] is first

    # change content AND bump mtime so the rescan re-imports it
    p.write_text(VALID_V2, encoding="utf-8")
    future = os.stat(p).st_mtime + 100
    os.utime(p, (future, future))
    loader.scan()
    assert loader.skills["alpha"] is not first
    assert loader.skills["alpha"].keywords == ["gamma"]

    # delete -> dropped from the registry
    p.unlink()
    loader.scan()
    assert "alpha" not in loader.skills


# -- manifest / matcher / run_skill --------------------------------------

def test_manifest_prompt_and_register_into_matcher(tmp_path):
    _write(tmp_path, "alpha.py", VALID)
    loader = SkillsLoader(skills_dir=tmp_path)
    loader.scan()

    assert "alpha: alpha, beta" in loader.manifest_prompt()

    m = Matcher()
    loader.register_into(m)
    action = m.match("please do alpha now")
    assert action == {"action": "skill", "args": {"name": "alpha"}, "say": "Running alpha."}


def test_run_skill_executes_and_passes_ctx(tmp_path):
    _write(tmp_path, "alpha.py", VALID)
    loader = SkillsLoader(skills_dir=tmp_path)
    loader.scan()
    result = loader.run_skill("alpha", "hello there")
    assert result == "ran alpha: hello there"


def test_run_unknown_skill_returns_graceful_string(tmp_path):
    loader = SkillsLoader(skills_dir=tmp_path)
    assert "haven't learned" in loader.run_skill("ghost", "x")


def test_run_skill_error_is_caught(tmp_path):
    _write(tmp_path, "boom.py",
           "from miko import helpers\n"
           'KEYWORDS = ["boom"]\n'
           "def run(ctx):\n"
           '    raise RuntimeError("kaboom")\n')
    loader = SkillsLoader(skills_dir=tmp_path)
    loader.scan()
    result = loader.run_skill("boom", "x")
    assert "kaboom" in result
