"""Tray icon and balloon notifications — the default UI, which is no UI.

NOK's job is to be a background daemon that does one thing when you knock. The
overlay HUD exists (`--overlay`) and is nice to look at, but it is the wrong
default: a permanently-running listener does not earn a piece of your screen,
and a HUD that appears for 400ms to say "Screenshot" is a heavier way of saying
what Windows already has a channel for.

So the default is the classic Windows shape for this: a tray icon, and a balloon
when something happens. On Windows 10/11 a balloon from a tray icon is rendered
as a real toast and lands in the Action Center, so the confirmation is still
there thirty seconds later when you look away and back.

The tray icon is also the honest thing to ship. An always-listening microphone
daemon with no window and no tray presence is indistinguishable from spyware;
this way there is always one visible, right-clickable thing saying NOK is on.

Everything here degrades to stdout. `Tray` is the only class that touches Win32,
it never raises out of a notification, and the message-formatting logic that
tests care about is pure and lives above it.
"""
import ctypes
import sys
from ctypes import wintypes

# Balloon field limits, from the NOTIFYICONDATAW struct. Windows silently
# truncates past these; doing it ourselves keeps the ellipsis readable.
TITLE_MAX = 63
TEXT_MAX = 255
TIP_MAX = 127


def fit(text: str, limit: int) -> str:
    """Trim to `limit` characters, ending in a single ellipsis when cut."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def action_message(intent: str, result: str) -> tuple[str, str]:
    """(title, body) for a completed instant action.

    The body is the concrete consequence -- where the file went, what opened --
    because "Screenshot taken" tells you nothing you did not already know from
    having knocked.
    """
    if intent == "screenshot":
        return "Screenshot saved", fit(result, TEXT_MAX)
    if intent == "media_play_pause":
        return "Play / pause", ""
    if intent == "open_fav_app":
        return fit(result, TITLE_MAX), ""
    return fit(intent.replace("_", " ").title(), TITLE_MAX), fit(result, TEXT_MAX)


# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------

WM_DESTROY = 0x0002
WM_APP_TRAY = 0x0400 + 1
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01
IDI_APPLICATION = 32512
HWND_MESSAGE = -3

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
) if sys.platform == "win32" else None


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC if WNDPROC else ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


def _declare(user32, kernel32):
    """Give ctypes the real signatures before calling anything.

    Not optional. ctypes defaults every unknown return type to `c_int`, so on
    64-bit Windows a returned HWND comes back with its top 32 bits cut off --
    the call succeeds, GetLastError says 0, and you are left holding a handle
    that is either zero or points at nothing. Every failure in this file that
    looked like "CreateWindowExW failed" was this.
    """
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
    user32.DefWindowProcW.restype = ctypes.c_long
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    ctypes.windll.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    ctypes.windll.shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATA),
    ]


class Tray:
    """A message-only window carrying one tray icon.

    Construction never raises: if any of the Win32 calls fail (or this is not
    Windows), `alive` stays False and every method falls back to printing. NOK
    losing its tray icon must not stop it answering knocks.
    """

    def __init__(self, tooltip="NOK — knock to act", on_quit=None):
        self.alive = False
        self.hwnd = None
        self._on_quit = on_quit
        self._nid = None
        self._proc_ref = None  # the WNDPROC must outlive the window
        if sys.platform != "win32":
            return
        try:
            self._create(tooltip)
            self.alive = True
        except Exception as exc:  # pragma: no cover - depends on the desktop
            from .logs import log_event
            log_event("tray_unavailable", error=str(exc))

    def _create(self, tooltip):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _declare(user32, kernel32)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_APP_TRAY and lparam in (WM_RBUTTONUP, WM_LBUTTONDBLCLK):
                if self._on_quit is not None:
                    self._on_quit()
                user32.PostQuitMessage(0)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc_ref = WNDPROC(wndproc)
        wc = _WNDCLASS()
        wc.lpfnWndProc = self._proc_ref
        wc.lpszClassName = "NokTrayWindow"
        wc.hInstance = kernel32.GetModuleHandleW(None)
        # A second run in the same process would fail to register; ignore that
        # and reuse the class, which is what the API does anyway.
        user32.RegisterClassW(ctypes.byref(wc))

        self.hwnd = user32.CreateWindowExW(
            0, "NokTrayWindow", "NOK", 0, 0, 0, 0, 0,
            wintypes.HWND(HWND_MESSAGE), None, wc.hInstance, None,
        )
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")

        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        nid.szTip = fit(tooltip, TIP_MAX)
        if not ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError("Shell_NotifyIconW(NIM_ADD) failed")
        self._nid = nid

    def balloon(self, title: str, text: str = "") -> None:
        """Show a notification. Never raises."""
        if not self.alive:
            print(f"  {title}" + (f" — {text}" if text else ""))
            return
        try:
            self._nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP | NIF_INFO
            self._nid.szInfoTitle = fit(title, TITLE_MAX)
            self._nid.szInfo = fit(text or " ", TEXT_MAX)
            self._nid.dwInfoFlags = NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception as exc:
            from .logs import log_event
            log_event("balloon_failed", error=str(exc))

    def run(self) -> None:
        """Pump messages until quit. This is the main thread's job when there
        is no overlay -- something has to own the message loop for the icon."""
        if not self.alive:
            import time
            while True:
                time.sleep(0.2)
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if not self.alive:
            return
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            ctypes.windll.user32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.alive = False
