"""Curated helper surface for teach-mode skills (Eng Review Amendment 14).

This is the ONLY miko module a teach-mode skill file is allowed to import.
Instead of a bare "you may not import" ban, skills get a small, blessed API:
the whitelisted actions re-exported as plain functions plus a couple of safe
utilities. The skill loader statically rejects any skill importing anything
beyond this module and stdlib `re` / `json`, so the whitelist boundary is the
only surface Codex-written code can reach.

The action helpers delegate to the live `Actions` toolbox, which is bound once
at startup via `bind(actions)`. Until then they raise `HelpersNotBound` so a
mis-wired skill fails loudly instead of silently doing nothing.
"""
import ctypes
import time

# A skill can never sleep the worker for longer than this (footgun guard).
MAX_SLEEP_S = 5.0

# The names a skill may use off this module (advertised in _template.py).
ALLOWED = (
    "open_app",
    "close_app",
    "type_text",
    "media",
    "screenshot",
    "sleep",
    "get_active_window_title",
)

_actions = None


class HelpersNotBound(RuntimeError):
    """Raised when an action helper is used before bind() wired the toolbox."""


def bind(actions) -> None:
    """Wire the live miko.actions.Actions instance the helpers delegate to.

    Called once by the app at startup. Safe to call again (e.g. in tests).
    """
    global _actions
    _actions = actions


def _require():
    if _actions is None:
        raise HelpersNotBound(
            "miko.helpers is not bound to an action toolbox; call helpers.bind()"
        )
    return _actions


# -- whitelisted actions, re-exported as flat functions ------------------

def open_app(name: str) -> str:
    """Launch a known app by name (from apps.json)."""
    return _require().open_app({"name": name})


def close_app(name: str) -> str:
    """Close a known app by its process name (from apps.json)."""
    return _require().close_app({"name": name})


def type_text(text: str) -> str:
    """Type text into the focused window."""
    return _require().type_text({"text": text})


def media(command: str) -> str:
    """Send a media/volume key: mute|play|pause|next|prev."""
    return _require().media({"command": command})


def screenshot() -> str:
    """Save a screenshot to ~/Pictures/miko/."""
    return _require().screenshot({})


# -- blessed utilities ---------------------------------------------------

def sleep(seconds: float) -> None:
    """Sleep, hard-capped at MAX_SLEEP_S so a skill can't hang the worker."""
    time.sleep(max(0.0, min(float(seconds), MAX_SLEEP_S)))


def get_active_window_title() -> str:
    """Best-effort title of the current foreground window (''  on failure)."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""
