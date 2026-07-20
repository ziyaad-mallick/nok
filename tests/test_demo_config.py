"""Guard the demo command surface: every app the mock brain can route to must
actually exist in apps.json (else a scripted mock phrase says "I don't have an
app called ..." mid-demo)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_mock_brain_open_app_targets_exist_in_apps_json():
    apps = json.loads((ROOT / "apps.json").read_text(encoding="utf-8"))
    mock = json.loads((ROOT / "tests" / "fixtures" / "mock_brain.json").read_text(encoding="utf-8"))
    referenced = {
        rule["response"]["args"]["name"]
        for rule in mock.get("rules", [])
        if rule.get("response", {}).get("action") == "open_app"
    }
    missing = referenced - set(apps)
    assert not missing, f"mock_brain references apps not in apps.json: {sorted(missing)}"


def test_macros_reference_known_apps():
    apps = json.loads((ROOT / "apps.json").read_text(encoding="utf-8"))
    macros = json.loads((ROOT / "macros.json").read_text(encoding="utf-8"))
    for name, steps in macros.items():
        for step in steps:
            if step.get("action") == "open_app":
                target = step["args"]["name"]
                assert target in apps, f"macro '{name}' opens unknown app '{target}'"
