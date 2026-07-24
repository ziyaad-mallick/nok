"""Media keys via SendInput.

All that survives of v1's action layer. The model used to receive a whitelist of
app-launching actions; that whitelist was 11 entries, 8 of them bare URLs, and it
is gone. What remains is the one local capability the product actually uses: tap
a media virtual-key, from the left double-knock and from the agent's
`media_control` tool.

Screenshots live in `screen.py`. There is no shell, no filesystem write and no
app launcher anywhere in the process.

The `_tap_vk` seam stays module-level so tests can patch at the OS boundary
without pressing real keys.
"""
import ctypes
import os
import time

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

_MEDIA_VK = {
    "mute": VK_VOLUME_MUTE,
    "play": VK_MEDIA_PLAY_PAUSE,   # the media key is a toggle: play == pause
    "pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "prev": VK_MEDIA_PREV_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
    "volume_up": VK_VOLUME_UP,
    "volume_down": VK_VOLUME_DOWN,
}

TYPE_PRE_DELAY = 0.12  # let the target window settle before typing into it


# INPUT structs for SendInput. Defined once at import; the user32 call happens
# only inside _tap_vk.
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
    """Press and release a virtual key."""
    if not hasattr(ctypes, "windll"):
        # MIKO is a Windows product, but a curious stranger on a Mac deserves a
        # sentence rather than a bare AttributeError out of the audio thread.
        raise RuntimeError("Media keys use the Windows SendInput API; "
                           "this action layer only runs on Windows.")
    down = _INPUT(type=_INPUT_KEYBOARD)
    down.ki = _KEYBDINPUT(vk, 0, 0, 0, None)
    up = _INPUT(type=_INPUT_KEYBOARD)
    up.ki = _KEYBDINPUT(vk, 0, _KEYEVENTF_KEYUP, 0, None)
    arr = (_INPUT * 2)(down, up)
    ctypes.windll.user32.SendInput(ctypes.c_uint(2), arr, ctypes.sizeof(_INPUT))


def media(command: str, repeat: int = 1) -> str:
    """Tap the media key for `command`. Returns a user-visible result string."""
    vk = _MEDIA_VK.get((command or "").strip().lower())
    if vk is None:
        return f"I don't know the media command '{command}'."
    for _ in range(max(1, min(10, int(repeat)))):
        _tap_vk(vk)
        time.sleep(0.03)
    return f"Media: {command}."


def _startfile(target: str) -> None:
    os.startfile(target)  # noqa: S606 - target is a name the user spoke aloud


def open_app(name: str) -> str:
    """Launch by name through the Windows shell.

    No whitelist. v1 shipped one with 11 entries, 8 of them bare URLs, and it
    was the single loudest complaint about the product ("it can't do anything
    except open Spotify"). The shell already resolves installed app names, URLs
    and protocol handlers; a curated list only ever subtracts from that.
    """
    name = (name or "").strip()
    if not name:
        return "I didn't catch what to open."
    if not hasattr(os, "startfile"):
        return "Opening apps by name only works on Windows."
    for target in (name, f"{name}.exe", f"{name}:"):
        try:
            _startfile(target)
            return f"Opening {name}."
        except OSError:
            continue
    return f"I couldn't find {name}."


def type_text(text: str) -> str:
    """Type into whatever window currently has focus."""
    text = text or ""
    if not text.strip():
        return "There was nothing to type."
    from pynput.keyboard import Controller

    time.sleep(TYPE_PRE_DELAY)
    Controller().type(text)
    preview = text if len(text) <= 40 else text[:37] + "..."
    return f"Typed: {preview}"
