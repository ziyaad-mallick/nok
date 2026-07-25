"""The preview must never be mistakable for the running app.

It renders the same overlay, in the same place, with the same states — but it
has no microphone, takes no screenshots and plays no audio. Someone who runs it
expecting the real thing sees an app that ignores their knocks, claims to take
screenshots it never takes, and replies inaudibly. That happened. These tests
keep the script honest about itself.
"""
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "overlay_preview.py"


@pytest.fixture(scope="module")
def source():
    return SCRIPT.read_text(encoding="utf-8")


def test_preview_script_exists(source):
    assert source.strip()


@pytest.mark.parametrize("claim", ["microphone", "screenshot", "audio"])
def test_banner_disclaims_each_faculty_it_lacks(source, claim):
    """Each of the three things a user reported as 'bugs' must be called out."""
    banner = re.search(r'BANNER\s*=\s*"""(.*?)"""', source, re.S)
    assert banner, "BANNER string missing"
    assert claim in banner.group(1).lower(), f"banner never mentions {claim}"


def test_banner_names_the_real_command(source):
    banner = re.search(r'BANNER\s*=\s*"""(.*?)"""', source, re.S).group(1)
    assert "-m nok" in banner, "banner must point at the real entry point"


def test_banner_is_actually_printed(source):
    assert re.search(r"print\(\s*BANNER\s*\)", source), "BANNER defined but never printed"


def test_preview_declares_itself_not_the_app(source):
    banner = re.search(r'BANNER\s*=\s*"""(.*?)"""', source, re.S).group(1).lower()
    assert "not the app" in banner or "scripted" in banner
