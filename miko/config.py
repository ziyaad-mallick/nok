"""Startup config loader + validator (Eng Review Amendment 6).

Loads and validates apps.json and macros.json BEFORE the pipeline starts.
Any schema problem or whitelist violation raises ConfigError naming the
file and the offending entry, so Miko refuses to launch misconfigured
rather than failing mid-demo.

apps.json schema:
    { "<name>": {"launch": "<path|uri>", "process": "<Image.exe>"?} , ... }

macros.json schema (steps must be a subset of the whitelist, and may not
themselves be "macro" or "skill" — no recursion, no undefined indirection):
    { "<name>": [ {"action": "<enum>", "args": {...}}, ... ], ... }
"""
import json
from pathlib import Path

from .jsonx import ACTION_WHITELIST

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Actions a macro step is allowed to invoke: the whitelist minus the two
# indirection actions (a macro of macros / skills is rejected up front).
MACRO_STEP_ACTIONS = tuple(a for a in ACTION_WHITELIST if a not in ("macro", "skill"))


class ConfigError(Exception):
    """Raised on any apps.json / macros.json schema or whitelist problem."""


def _load_json(path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{p}: file not found")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{p}: invalid JSON ({exc})")
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top-level value must be an object")
    return data


def load_apps(path) -> dict:
    """Validate apps.json and return the app map."""
    data = _load_json(path)
    for name, entry in data.items():
        where = f"{path}: app '{name}'"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: entry must be an object")
        launch = entry.get("launch")
        if not isinstance(launch, str) or not launch.strip():
            raise ConfigError(f"{where}: missing non-empty 'launch' string")
        process = entry.get("process", "")
        if not isinstance(process, str):
            raise ConfigError(f"{where}: 'process' must be a string")
    return data


def load_macros(path) -> dict:
    """Validate macros.json (steps subset of the whitelist) and return it."""
    data = _load_json(path)
    for name, steps in data.items():
        where = f"{path}: macro '{name}'"
        if not isinstance(steps, list) or not steps:
            raise ConfigError(f"{where}: must be a non-empty list of steps")
        for i, step in enumerate(steps):
            sw = f"{where} step {i}"
            if not isinstance(step, dict):
                raise ConfigError(f"{sw}: step must be an object")
            action = step.get("action")
            if action not in MACRO_STEP_ACTIONS:
                raise ConfigError(
                    f"{sw}: action '{action}' not allowed in a macro "
                    f"(allowed: {MACRO_STEP_ACTIONS})"
                )
            args = step.get("args", {})
            if not isinstance(args, dict):
                raise ConfigError(f"{sw}: 'args' must be an object")
    return data


def load_config(apps_path=None, macros_path=None):
    """Load + validate both config files. Returns (apps, macros).

    Defaults are anchored to the repo root (not cwd) so `python -m miko` works
    from any working directory.
    """
    apps_path = apps_path if apps_path is not None else _REPO_ROOT / "apps.json"
    macros_path = macros_path if macros_path is not None else _REPO_ROOT / "macros.json"
    return load_apps(apps_path), load_macros(macros_path)
