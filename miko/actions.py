"""Whitelisted action toolbox.

The model NEVER gets free-form execution: it may only emit an action from
the whitelist, and each maps to one hand-written function here. Every action
returns a short, user-visible result string (spoken/toasted) and never raises
past its own body for expected failures.

Live toolbox (OV-3 default = 3 actions): open_app, type_text, media.
Stretch (implemented, advertised as stretch in the prompt): close_app,
screenshot, macro.

Each OS call goes through a tiny module-level seam (_startfile, _run, _type,
_tap_vk, _grab_screenshot) so tests can patch at the OS boundary without
touching real apps/keys/screens.
"""
import ctypes
import subprocess
import time
from datetime import datetime
from pathlib import Path

# -- media virtual-key codes (SendInput) ---------------------------------
VK_VOLUME_MUTE = 0xAD
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

_MEDIA_VK = {
    "mute": VK_VOLUME_MUTE,
    "play": VK_MEDIA_PLAY_PAUSE,   # media key is a toggle: play == pause
    "pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "prev": VK_MEDIA_PREV_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
}

TYPE_PRE_DELAY = 0.15  # let the target window regain focus before typing


# -- OS-boundary seams (patched in tests) --------------------------------

def _startfile(target: str) -> None:
    """Launch a path/URI via the Windows shell (handles spotify:, https:, .exe)."""
    import os

    os.startfile(target)  # noqa: S606 - target comes from validated apps.json only


def _run(cmd: list) -> None:
    subprocess.run(cmd, capture_output=True, timeout=10)


def _type(text: str) -> None:
    from pynput.keyboard import Controller

    time.sleep(TYPE_PRE_DELAY)
    Controller().type(text)


# INPUT structs for SendInput (keyboard events). Defined once at import; the
# actual user32 call happens only inside _tap_vk.
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002


def _tap_vk(vk: int) -> None:
    """Press and release a virtual key via SendInput."""
    down = _INPUT(type=_INPUT_KEYBOARD)
    down.ki = _KEYBDINPUT(vk, 0, 0, 0, None)
    up = _INPUT(type=_INPUT_KEYBOARD)
    up.ki = _KEYBDINPUT(vk, 0, _KEYEVENTF_KEYUP, 0, None)
    n = ctypes.c_uint(2)
    arr = (_INPUT * 2)(down, up)
    ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(_INPUT))


def _grab_screenshot(dest: Path) -> None:
    import mss
    import mss.tools

    with mss.mss() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(monitor)
        mss.tools.to_png(shot.rgb, shot.size, output=str(dest))


# -- toolbox -------------------------------------------------------------

class Actions:
    """Holds the validated apps/macros maps and exposes one method per action.

    Methods take the action's `args` dict and return a result string. `macro`
    takes a step-runner callback (the dispatcher's internal executor) so a
    macro dispatches its steps back through the same whitelist path.
    """

    def __init__(self, apps: dict, macros: dict):
        self._apps = apps
        self._macros = macros

    def open_app(self, args: dict) -> str:
        name = (args.get("name") or "").strip().lower()
        entry = self._apps.get(name)
        if entry is None:
            return f"I don't have an app called '{name}'."
        _startfile(entry["launch"])
        return f"Opening {name}."

    def close_app(self, args: dict) -> str:
        name = (args.get("name") or "").strip().lower()
        entry = self._apps.get(name)
        if entry is None:
            return f"I don't have an app called '{name}'."
        process = entry.get("process", "")
        if not process:
            return f"I don't know the process name for {name}, so I can't close it."
        _run(["taskkill", "/IM", process, "/F"])
        return f"Closing {name}."

    def type_text(self, args: dict) -> str:
        text = args.get("text", "")
        if not text:
            return "There was nothing to type."
        _type(text)
        preview = text if len(text) <= 40 else text[:37] + "..."
        return f"Typed: {preview}"

    def media(self, args: dict) -> str:
        command = (args.get("command") or "").strip().lower()
        vk = _MEDIA_VK.get(command)
        if vk is None:
            return f"I don't know the media command '{command}'."
        _tap_vk(vk)
        return f"Media: {command}."

    def screenshot(self, args: dict) -> str:
        folder = Path.home() / "Pictures" / "miko"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"miko-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        _grab_screenshot(dest)
        return f"Saved a screenshot to {dest}."

    def run_macro(self, args: dict, step_runner) -> str:
        name = (args.get("name") or "").strip()
        steps = self._macros.get(name)
        if steps is None:
            return f"I don't have a macro called '{name}'."
        for step in steps:
            step_runner(step)
        return f"Ran macro '{name}'."


# The toolbox description advertised in the model prompt (brain.py).
TOOLBOX_PROMPT = (
    "Available actions (return exactly one as JSON):\n"
    '  open_app  {"name": "<app>"}      - launch a known app\n'
    '  type_text {"text": "<text>"}     - type text into the focused window\n'
    '  media     {"command": "mute|play|pause|next|prev"} - media/volume key\n'
    "Stretch actions (use only if clearly requested):\n"
    '  close_app {"name": "<app>"} | screenshot {} | '
    'macro {"name": "<macro>"} | skill {"name": "<skill>"}'
)
