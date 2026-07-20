"""Miko on-screen overlay — the "Ember pill" HUD.

Three layers, one file, one design surface:

  * ``OverlayModel`` — pure, headless, unit-testable state+animation logic.
    Every field it produces is a deterministic function of ``now`` (a
    ``time.monotonic()`` reading) and the model's ``state_entered_at``, so a
    dropped render frame can never desync the animation and the whole thing is
    testable with an injected clock. It imports NO tkinter.

  * ``Overlay`` — the tkinter window. A worker (detector) thread calls the
    thread-safe, non-blocking ``set_state`` which only enqueues. The main
    thread runs a single ~30fps ``root.after`` loop that drains the queue,
    advances the model, and repaints. tkinter and ctypes are imported lazily
    inside the window class only, so ``from miko.overlay import OverlayModel``
    works headless (CI, unit tests, ``python -m miko`` on a box with no display).

The overlay is a passive subscriber: it receives ``(state, detail)`` tuples via
``App.observer`` and never calls back into the app.
"""
import math
import queue
import time

from .logs import log_event

# ---------------------------------------------------------------------------
# Design tokens — "Ember": a warm, muted family (cooling ash -> glowing coal)
# instead of a stock UI-kit swatch per state. One coherent value/chroma
# curve so every accent still reads as part of the same object.
# ---------------------------------------------------------------------------
PANEL = "#15120E"
TEXT_PRIMARY = "#F4EEE4"
TEXT_SECONDARY = "#A79C8C"
TEXT_TRANSCRIPT = "#E6DCC8"
SHADOW = "#0A0705"       # crisp offset drop-shadow sliver (no blur on Canvas)
CORNER_RADIUS = 24       # near-full pill at compact height, soft card at expanded
KEY = "#010203"  # transparent-color key, never used in the palette

# Per-state accent — one hue each, never reused.
_ACCENTS = {
    "SLEEPING": "#726A5C",   # cooling ash — idle
    "LISTENING": "#3FD1C0",  # bright teal — ears open
    "THINKING": "#9B80E8",   # amethyst — turning it over
    "ACTING": "#5BC98A",     # jade — doing it
    "TEACHING": "#E8A83D",   # warm gold — a spark of new skill
    "ERROR": "#E1604A",      # glowing coal-red — something's wrong
    "DONE": "#5BC98A",       # same hue as ACTING — landed cleanly
    "NEUTRAL": "#8C8175",
}

_THINK_VERBS = ["Thinking", "Consulting codex", "Planning the action"]

# Two sizes.
_COMPACT = (380, 64)
_EXPANDED = (560, 104)
_EXPANDED_MODES = frozenset({"THINKING", "TEACHING", "ACTING", "ERROR", "DONE"})

# Timing constants (seconds).
SIZE_EASE = 0.18       # size change eases over ~180ms
DONE_HOLD = 1.8        # DONE resolve holds before fading to SLEEPING
ERROR_STICKY = 4.0     # hold the error visual this long since ERROR entered
ERROR_MAX_LINK = 6.0   # only treat SLEEPING as an error-release if within this
SLEEP_DIM_AFTER = 6.0  # SLEEPING dims alpha after this many idle seconds


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------
def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def _ease_out_cubic(x):
    x = _clamp(x)
    return 1.0 - (1.0 - x) ** 3


def _is_expanded(mode):
    return mode in _EXPANDED_MODES


def _dims(expanded):
    return _EXPANDED if expanded else _COMPACT


def _dots(elapsed):
    """Animated-ellipsis dot count (0..3), cycling every 400ms."""
    return int(elapsed / 0.4 + 1e-9) % 4


def _timer(prefix, elapsed):
    secs = int(elapsed)
    return f"{prefix} · {secs // 60}:{secs % 60:02d}"


def _wake_line(detail):
    up = (detail or "").upper()
    if "TRIPLE" in up:
        return "woke on triple knock"
    return "woke on double knock"


def lerp_hex(a, b, p):
    """Blend hex colour ``a`` toward ``b`` (Canvas has no per-item alpha, so
    colour-lerp toward the panel IS the fade)."""
    p = _clamp(p)
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = round(ar + (br - ar) * p)
    g = round(ag + (bg - ag) * p)
    bl = round(ab + (bb - ab) * p)
    return f"#{r:02x}{g:02x}{bl:02x}"


# ---------------------------------------------------------------------------
# OverlayModel — pure headless logic
# ---------------------------------------------------------------------------
class OverlayModel:
    """Holds the current display mode and produces a full render description
    from a ``now`` reading. No tkinter; no wall-clock reads inside — the caller
    injects ``now`` so it is fully deterministic and unit-testable."""

    def __init__(self):
        self._mode = "SLEEPING"
        self.state_entered_at = 0.0
        self.detail = ""
        self.transcript = ""

        # last RAW (real) state/detail applied — drives no-op de-dupe and the
        # DONE / sticky-error synthesis.
        self._raw_state = None
        self._raw_detail = None

        # size easing bookkeeping
        self._prev_expanded = False
        self._expanded_current = False

        # synthetic-transition bookkeeping
        self._error_release_at = None
        self._pending_sleep_detail = "cycle complete"

    # -- transitions -------------------------------------------------------

    def _enter(self, mode, detail, now):
        self._prev_expanded = self._expanded_current
        self._mode = mode
        self.detail = detail
        self.state_entered_at = now
        self._expanded_current = _is_expanded(mode)
        self._error_release_at = None

    def apply(self, state, detail=None, now=0.0):
        """Advance the model in response to a ``(state, detail)`` event.

        Idempotent for an identical repeated event (the animation clock is not
        restarted). Unknown state strings render a neutral card. ``DONE`` is
        synthesised (overlay-only) when a completed ACTING/TEACHING cycle
        reports back to SLEEPING; ERROR is held sticky across its trailing
        SLEEPING."""
        detail = str(detail or "")
        if state == self._raw_state and detail == self._raw_detail:
            return  # same event again -> do not restart the animation
        prev_real = self._raw_state
        self._raw_state = state
        self._raw_detail = detail

        if state == "SLEEPING":
            if prev_real == "ERROR":
                age = now - self.state_entered_at
                if detail == "cycle complete" and age <= ERROR_MAX_LINK:
                    # STICKY: keep showing the error; view() fades it out at
                    # ERROR_STICKY seconds since the error was entered.
                    self._error_release_at = self.state_entered_at + ERROR_STICKY
                    self._pending_sleep_detail = detail
                    return
                self._enter("SLEEPING", detail, now)
                return
            if prev_real in ("ACTING", "TEACHING") and detail == "cycle complete":
                self._pending_sleep_detail = detail
                self._enter("DONE", detail, now)
                return
            self._enter("SLEEPING", detail, now)
            return

        if state == "LISTENING":
            self._enter("LISTENING", detail, now)
            return
        if state == "THINKING":
            self.transcript = detail
            self._enter("THINKING", detail, now)
            return
        if state == "TEACHING":
            self.transcript = detail
            self._enter("TEACHING", detail, now)
            return
        if state == "ACTING":
            self._enter("ACTING", detail, now)
            return
        if state == "ERROR":
            self._enter("ERROR", detail, now)
            return

        # unknown -> neutral card, never crash
        self._enter("NEUTRAL", detail, now)

    # -- effective mode (handles the two time-driven auto-transitions) -----

    def _effective(self, now):
        """Return (eff_mode, anchor, from_expanded, to_expanded).

        ``anchor`` is when the effective mode began, so animations stay pure.
        DONE auto-resolves to SLEEPING after DONE_HOLD; a sticky ERROR
        auto-resolves to SLEEPING at its release time — both without any
        further ``apply`` call."""
        m = self._mode
        e = self.state_entered_at
        if m == "DONE":
            if now < e + DONE_HOLD:
                return ("DONE", e, self._prev_expanded, True)
            return ("SLEEPING", e + DONE_HOLD, True, False)
        if m == "ERROR" and self._error_release_at is not None:
            if now >= self._error_release_at:
                return ("SLEEPING", self._error_release_at, True, False)
            return ("ERROR", e, self._prev_expanded, self._expanded_current)
        return (m, e, self._prev_expanded, self._expanded_current)

    # -- render description ------------------------------------------------

    def view(self, now):
        eff_mode, anchor, from_exp, to_exp = self._effective(now)
        elapsed = now - anchor
        detail = self._pending_sleep_detail if eff_mode != self._mode else self.detail

        size_progress = _ease_out_cubic((now - anchor) / SIZE_EASE)
        fw, fh = _dims(from_exp)
        tw, th = _dims(to_exp)
        width = fw + (tw - fw) * size_progress
        height = fh + (th - fh) * size_progress

        title, dots, line2, style, timer = self._labels(eff_mode, detail, elapsed)

        return {
            "mode": eff_mode,
            "accent": _ACCENTS.get(eff_mode, _ACCENTS["NEUTRAL"]),
            "title": title,
            "ellipsis_dots": dots,
            "line2": line2,
            "line2_style": style,
            "line2_progress": _clamp(elapsed / 0.15),  # quote "drop" slide
            "timer_text": timer,
            "expanded": to_exp,
            "size_progress": size_progress,
            "width": width,
            "height": height,
            "orb": self._orb(eff_mode, elapsed),
            "rings": self._rings(eff_mode, elapsed),
            "shake_px": self._shake(eff_mode, elapsed),
            "window_alpha": self._alpha(eff_mode, elapsed),
            "detail": detail,
        }

    # -- per-mode text -----------------------------------------------------

    def _labels(self, mode, detail, elapsed):
        dots = 0
        timer = None
        line2 = ""
        style = "plain"
        if mode == "SLEEPING":
            title = "MIKO"
            if detail.startswith("ready") and elapsed < 5.0:
                line2 = "ready — knock twice"
            else:
                line2 = "knock twice"
        elif mode == "LISTENING":
            title = "Listening"
            dots = _dots(elapsed)
            line2 = _wake_line(detail)
        elif mode == "THINKING":
            title = _THINK_VERBS[int(elapsed // 4.0) % len(_THINK_VERBS)]
            dots = _dots(elapsed)
            line2 = '"' + self.transcript + '"'
            style = "transcript"
            timer = _timer("thinking", elapsed)
        elif mode == "TEACHING":
            title = "Learning a new trick"
            dots = _dots(elapsed)
            line2 = '"' + self.transcript + '"'
            style = "transcript"
            timer = _timer("learning", elapsed)
        elif mode == "ACTING":
            title = "Acting"
            line2 = "→ " + detail
            style = "chip"
        elif mode == "ERROR":
            title = "Something broke"
            line2 = detail
        elif mode == "DONE":
            title = "Done"
        else:  # NEUTRAL / unknown
            title = "Miko"
            line2 = detail
        return title, dots, line2, style, timer

    # -- per-mode orb ------------------------------------------------------

    def _orb(self, mode, elapsed):
        if mode == "SLEEPING":
            scale = 1.04 + 0.04 * math.sin(2 * math.pi * elapsed / 3.2)
            return {"kind": "breathe", "phase": (elapsed % 3.2) / 3.2, "scale": scale}
        if mode == "LISTENING":
            scale = 1.075 + 0.075 * math.sin(2 * math.pi * elapsed / 1.0)
            return {"kind": "pulse", "phase": elapsed % 1.0, "scale": scale}
        if mode == "THINKING":
            # a slower revolution with a breathing arc-length (material-style
            # spinner) reads as deliberate work, not a frantic spin — this can
            # be on screen for 17-38s so it has to stay calm and alive.
            angle = (elapsed / 1.6 * 360.0) % 360.0
            extent = 70.0 + 50.0 * (0.5 + 0.5 * math.sin(2 * math.pi * elapsed / 2.4))
            return {"kind": "arc", "phase": angle, "scale": 1.0, "extent": extent}
        if mode == "ACTING":
            scale = 1.0 + 0.3 * max(0.0, 1.0 - elapsed / 0.12)
            return {"kind": "flash", "phase": _clamp(elapsed / 0.12), "scale": scale}
        if mode == "TEACHING":
            # a slow breathe layered with a faster, smaller flicker — reads as
            # a candle/spark rather than the uniform breathe used for SLEEPING.
            base = 1.05 + 0.035 * math.sin(2 * math.pi * elapsed / 1.7)
            flicker = 0.015 * math.sin(2 * math.pi * elapsed / 0.35 + 1.3)
            return {"kind": "write", "phase": (elapsed % 1.7) / 1.7, "scale": base + flicker}
        if mode == "ERROR":
            return {"kind": "alert", "phase": 0.0, "scale": 1.0}
        if mode == "DONE":
            return {"kind": "check", "phase": _clamp(elapsed / 0.25), "scale": 1.0}
        return {"kind": "dot", "phase": 0.0, "scale": 1.0}

    # -- LISTENING sonar rings --------------------------------------------

    def _rings(self, mode, elapsed):
        if mode != "LISTENING":
            return []
        interval = 0.9
        rings = []
        k = int(elapsed / interval)
        for j in (k, k - 1):
            if j < 0:
                continue
            age = elapsed - j * interval
            if 0.0 <= age < interval:
                p = age / interval
                rings.append({
                    "radius": 14.0 + (26.0 - 14.0) * p,
                    "color": lerp_hex(_ACCENTS["LISTENING"], PANEL, p),
                })
        return rings

    # -- ERROR shake -------------------------------------------------------

    def _shake(self, mode, elapsed):
        if mode == "ERROR" and elapsed < 0.35:
            return 8.0 * math.sin(40.0 * elapsed) * math.exp(-elapsed / 0.12)
        return 0.0

    # -- window alpha ------------------------------------------------------

    def _alpha(self, mode, elapsed):
        if mode == "SLEEPING" and elapsed > SLEEP_DIM_AFTER:
            # idle recedes a little further into the background than before —
            # the HUD should visibly rest, not just sit at a flat 97%.
            return 0.97 + (0.85 - 0.97) * _clamp((elapsed - SLEEP_DIM_AFTER) / 1.0)
        return 0.97


# ===========================================================================
# Overlay — tkinter window (main thread). tkinter/ctypes imported lazily.
# ===========================================================================
class Overlay:
    """Bottom-center, always-on-top, click-through Ember-pill HUD.

    If Tk() cannot be created (no display), the overlay degrades to a no-op
    that still drains the queue, so ``python -m miko`` keeps running headless.
    """

    _FONT_TITLE = ("Segoe UI", 15, "bold")
    _FONT_LABEL = ("Segoe UI", 10, "bold")
    _FONT_TRANSCRIPT = ("Segoe UI", 13, "italic")
    _FONT_GLYPH = ("Segoe UI", 14, "bold")  # bigger than the label font so the
                                             # ERROR "!" actually reads at a glance

    def __init__(self, model=None):
        self.model = model or OverlayModel()
        self._queue = queue.Queue()
        self._root = None
        self._canvas = None
        self._items = {}
        self._closed = False
        self._last_topmost = 0.0
        # window geometry: fixed at the expanded size, pill drawn centered
        # within so size changes never thrash window geometry (only shake does).
        self._win_w = _EXPANDED[0] + 8
        self._win_h = _EXPANDED[1] + 16
        self._base_x = 0
        self._base_y = 0

        try:
            self._build()
        except Exception as exc:  # no display / tkinter unavailable -> no-op
            log_event("overlay_init_failed", error=str(exc))
            self._root = None

    # -- thread-safe input (called from the detector/worker thread) --------

    def set_state(self, state, detail=""):
        """Enqueue a state event. Thread-safe, non-blocking, never touches
        tkinter."""
        try:
            self._queue.put_nowait((state, str(detail or ""), time.monotonic()))
        except Exception as exc:  # a full/broken queue must not crash the cycle
            log_event("overlay_enqueue_failed", error=str(exc))

    # -- lifecycle ---------------------------------------------------------

    def run(self):
        if self._root is None:
            self._headless_drain()
            return
        try:
            self._root.mainloop()
        except Exception as exc:
            log_event("overlay_mainloop_error", error=str(exc))

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass

    def _headless_drain(self):
        """Degraded mode: no window, just keep the queue from backing up so the
        rest of the app runs normally until interrupted."""
        while not self._closed:
            try:
                self._queue.get(timeout=0.2)
            except queue.Empty:
                pass

    # -- window construction ----------------------------------------------

    def _build(self):
        import ctypes
        import tkinter as tk

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.97)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        root.configure(bg=KEY)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        self._base_x = (sw - self._win_w) // 2
        self._base_y = sh - self._win_h - 96
        root.geometry(f"{self._win_w}x{self._win_h}+{self._base_x}+{self._base_y}")

        canvas = tk.Canvas(
            root, width=self._win_w, height=self._win_h,
            highlightthickness=0, bg=KEY, bd=0,
        )
        canvas.pack(fill="both", expand=True)

        self._root = root
        self._canvas = canvas
        self._tk = tk
        self._create_items()

        root.update_idletasks()
        self._make_click_through()
        root.after(33, self._tick)

    def _make_click_through(self):
        """Set WS_EX_LAYERED|TRANSPARENT|TOOLWINDOW so clicks pass through and
        the window is hidden from alt-tab."""
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            if not hwnd:
                hwnd = self._root.winfo_id()
            get_long = ctypes.windll.user32.GetWindowLongPtrW
            set_long = ctypes.windll.user32.SetWindowLongPtrW
            style = get_long(hwnd, GWL_EXSTYLE)
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
            set_long(hwnd, GWL_EXSTYLE, style)
        except Exception as exc:
            log_event("overlay_exstyle_failed", error=str(exc))

    def _create_items(self):
        """Create every canvas item ONCE; per-frame we only itemconfigure/coords
        (no delete/recreate -> flicker-free)."""
        c = self._canvas
        # crisp offset shadow first (bottom of the stack) so the pill reads as
        # a lifted card rather than a flat sticker on arbitrary wallpaper.
        self._items["shadow"] = c.create_polygon(
            0, 0, 0, 0, 0, 0, 0, 0, fill=SHADOW, outline="", smooth=True,
        )
        self._items["pill"] = c.create_polygon(
            0, 0, 0, 0, 0, 0, 0, 0, fill=PANEL, outline="", smooth=True,
        )
        self._items["chip"] = c.create_rectangle(
            0, 0, 0, 0, fill=PANEL, outline="", state="hidden",
        )
        # orb: three nested, ever-brighter discs faked-gradient the halo (no
        # blur on Canvas), then the solid core — the one signature element.
        self._items["halo3"] = c.create_oval(0, 0, 0, 0, fill=PANEL, outline="")
        self._items["halo2"] = c.create_oval(0, 0, 0, 0, fill=PANEL, outline="")
        self._items["glow"] = c.create_oval(0, 0, 0, 0, fill=PANEL, outline="")
        self._items["orb"] = c.create_oval(0, 0, 0, 0, fill=PANEL, outline="")
        self._items["arc"] = c.create_arc(
            0, 0, 0, 0, style="arc", width=3, outline=PANEL, state="hidden",
        )
        self._items["ring1"] = c.create_oval(0, 0, 0, 0, outline=KEY, state="hidden")
        self._items["ring2"] = c.create_oval(0, 0, 0, 0, outline=KEY, state="hidden")
        self._items["check"] = c.create_line(
            0, 0, 0, 0, 0, 0, fill=PANEL, width=3, state="hidden",
        )
        self._items["glyph"] = c.create_text(
            0, 0, text="", fill=TEXT_PRIMARY, font=self._FONT_LABEL, state="hidden",
        )
        # text columns
        self._items["title"] = c.create_text(
            0, 0, text="", anchor="w", fill=TEXT_PRIMARY, font=self._FONT_TITLE,
        )
        self._items["state"] = c.create_text(
            0, 0, text="", anchor="w", fill=TEXT_SECONDARY, font=self._FONT_LABEL,
        )
        self._items["line2"] = c.create_text(
            0, 0, text="", anchor="w", fill=TEXT_SECONDARY, font=self._FONT_LABEL,
        )
        self._items["timer"] = c.create_text(
            0, 0, text="", anchor="e", fill=TEXT_SECONDARY, font=self._timer_font(),
        )

    def _timer_font(self):
        # Cascadia Mono with a Consolas fallback.
        import tkinter.font as tkfont
        try:
            families = set(tkfont.families(self._root))
        except Exception:
            families = set()
        family = "Cascadia Mono" if "Cascadia Mono" in families else "Consolas"
        return (family, 11)

    # -- the single 30fps loop --------------------------------------------

    def _tick(self):
        if self._closed or self._root is None:
            return
        try:
            self._drain()
            now = time.monotonic()
            v = self.model.view(now)
            self._paint(v, now)
        except Exception as exc:
            log_event("overlay_tick_error", error=str(exc))
        try:
            self._root.after(33, self._tick)
        except Exception:
            pass

    def _drain(self):
        while True:
            try:
                state, detail, ts = self._queue.get_nowait()
            except queue.Empty:
                break
            self.model.apply(state, detail, ts)

    def _rounded_points(self, x0, y0, x1, y1, r):
        # eight corner anchors; smooth=True rounds them into a pill.
        return [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]

    def _paint(self, v, now):
        c = self._canvas
        it = self._items
        accent = v["accent"]

        # re-assert topmost every ~2s
        if now - self._last_topmost > 2.0:
            self._last_topmost = now
            try:
                self._root.attributes("-topmost", True)
            except Exception:
                pass

        try:
            self._root.attributes("-alpha", v["window_alpha"])
        except Exception:
            pass

        # shake moves the whole window horizontally
        x = self._base_x + int(round(v["shake_px"]))
        try:
            self._root.geometry(f"{self._win_w}x{self._win_h}+{x}+{self._base_y}")
        except Exception:
            pass

        w, h = v["width"], v["height"]
        cx = self._win_w / 2
        cy = self._win_h / 2
        left = cx - w / 2
        top = cy - h / 2
        right = cx + w / 2
        bottom = cy + h / 2

        # shadow sits 3px lower than the pill so a sliver peeks out at the
        # bottom edge — a cheap but legible stand-in for a blurred drop shadow.
        c.coords(it["shadow"], *self._rounded_points(left, top + 3, right, bottom + 3, CORNER_RADIUS))
        c.coords(it["pill"], *self._rounded_points(left, top, right, bottom, CORNER_RADIUS))
        if v["mode"] in ("DONE", "ERROR"):
            outline_color, outline_w = accent, 2
        else:
            # quiet neutral hairline year-round so the card keeps a defined
            # edge on both very light and very dark desktop wallpaper.
            outline_color, outline_w = lerp_hex(TEXT_SECONDARY, PANEL, 0.55), 1
        c.itemconfigure(it["pill"], outline=outline_color, width=outline_w)

        # orb column: layered halo (dim -> bright) faking a radial glow,
        # topped with the solid core.
        orb = v["orb"]
        orad = 13.0 * orb["scale"]
        ocx = left + 34
        ocy = cy
        c.coords(it["halo3"], ocx - orad - 12, ocy - orad - 12, ocx + orad + 12, ocy + orad + 12)
        c.itemconfigure(it["halo3"], fill=lerp_hex(accent, PANEL, 0.82))
        c.coords(it["halo2"], ocx - orad - 7, ocy - orad - 7, ocx + orad + 7, ocy + orad + 7)
        c.itemconfigure(it["halo2"], fill=lerp_hex(accent, PANEL, 0.6))
        c.coords(it["glow"], ocx - orad - 3, ocy - orad - 3, ocx + orad + 3, ocy + orad + 3)
        c.itemconfigure(it["glow"], fill=lerp_hex(accent, PANEL, 0.3))
        c.coords(it["orb"], ocx - orad, ocy - orad, ocx + orad, ocy + orad)
        c.itemconfigure(it["orb"], fill=accent)

        self._paint_orb_extras(orb, ocx, ocy, orad, accent, v)
        self._paint_rings(v["rings"], ocx, ocy)

        # text columns
        tx = left + 62
        dots = "." * v["ellipsis_dots"]
        c.coords(it["title"], tx, cy - (16 if v["expanded"] else 0))
        c.itemconfigure(it["title"], text=v["title"] + dots, fill=TEXT_PRIMARY)

        if v["expanded"]:
            c.itemconfigure(it["state"], state="hidden")
            self._paint_line2(v, tx, cy + 16, left, right)
        else:
            c.itemconfigure(it["state"], state="normal")
            c.coords(it["state"], tx, cy + 16)
            c.itemconfigure(it["state"], text=v["line2"], fill=TEXT_SECONDARY)
            c.itemconfigure(it["line2"], state="hidden")
            c.itemconfigure(it["chip"], state="hidden")

        if v["timer_text"]:
            c.coords(it["timer"], right - 20, cy - 16)
            c.itemconfigure(it["timer"], text=v["timer_text"], state="normal",
                            fill=lerp_hex(accent, TEXT_SECONDARY, 0.4))
        else:
            c.itemconfigure(it["timer"], state="hidden")

    def _paint_line2(self, v, tx, ty, left, right):
        c = self._canvas
        it = self._items
        style = v["line2_style"]
        if style == "chip":
            c.itemconfigure(it["line2"], state="hidden")
            text = v["line2"]
            # tint the chip from the state's own accent instead of a fixed
            # colour, so it stays coherent whichever mode ever uses "chip".
            c.itemconfigure(it["chip"], state="normal", fill=lerp_hex(v["accent"], PANEL, 0.72))
            # size the chip to the text
            tmp = c.create_text(tx + 8, ty, text=text, anchor="w",
                                font=self._timer_font())
            bx = c.bbox(tmp)
            c.delete(tmp)
            if bx:
                c.coords(it["chip"], bx[0] - 8, bx[1] - 4, bx[2] + 8, bx[3] + 4)
            c.itemconfigure(it["line2"], state="normal", text=text,
                            fill=v["accent"], font=self._timer_font())
            c.coords(it["line2"], tx + 8, ty)
        else:
            c.itemconfigure(it["chip"], state="hidden")
            if style == "transcript":
                fill = TEXT_TRANSCRIPT
                font = self._FONT_TRANSCRIPT
                # subtle up-slide on the quote "drop"
                ty = ty + int(round((1.0 - v["line2_progress"]) * 6))
            else:
                fill = TEXT_SECONDARY
                font = self._FONT_LABEL
            c.itemconfigure(it["line2"], state="normal", text=v["line2"],
                            fill=fill, font=font)
            c.coords(it["line2"], tx, ty)

    def _paint_orb_extras(self, orb, ocx, ocy, orad, accent, v):
        c = self._canvas
        it = self._items
        for key in ("arc", "check", "glyph"):
            c.itemconfigure(it[key], state="hidden")
        kind = orb["kind"]
        if kind == "arc":
            c.itemconfigure(it["glow"], fill=lerp_hex(accent, PANEL, 0.5))
            c.coords(it["arc"], ocx - orad, ocy - orad, ocx + orad, ocy + orad)
            c.itemconfigure(it["arc"], start=orb["phase"], extent=orb.get("extent", 90),
                            outline=accent, state="normal")
            c.itemconfigure(it["orb"], fill=PANEL)
        elif kind == "check":
            p = orb["phase"]
            # two-segment tick drawn over 250ms; stroke derives from the
            # accent (darkened) so it stays legible against the bright core
            # regardless of which state's accent lands here.
            dark_stroke = lerp_hex(accent, "#000000", 0.78)
            x0, y0 = ocx - 6, ocy
            x1, y1 = ocx - 1, ocy + 5
            x2, y2 = ocx + 7, ocy - 5
            mx = x1 + (x2 - x1) * _clamp((p - 0.5) / 0.5)
            my = y1 + (y2 - y1) * _clamp((p - 0.5) / 0.5)
            if p < 0.5:
                bx = x0 + (x1 - x0) * (p / 0.5)
                by = y0 + (y1 - y0) * (p / 0.5)
                c.coords(it["check"], x0, y0, bx, by, bx, by)
            else:
                c.coords(it["check"], x0, y0, x1, y1, mx, my)
            c.itemconfigure(it["check"], fill=dark_stroke, state="normal")
        elif kind == "alert":
            # BUG FIX: this used to draw a near-black "!" on a near-black orb
            # (both ~#0B0B0B) — effectively invisible. The mark now glows in
            # the accent colour against the hollow dark core, like a hot coal.
            c.itemconfigure(it["glyph"], text="!", state="normal",
                            fill=accent, font=self._FONT_GLYPH)
            c.coords(it["glyph"], ocx, ocy)
            c.itemconfigure(it["orb"], fill=PANEL)
            c.itemconfigure(it["glow"], fill=accent)

    def _paint_rings(self, rings, ocx, ocy):
        c = self._canvas
        it = self._items
        for i, key in enumerate(("ring1", "ring2")):
            if i < len(rings):
                r = rings[i]["radius"]
                c.coords(it[key], ocx - r, ocy - r, ocx + r, ocy + r)
                c.itemconfigure(it[key], outline=rings[i]["color"], state="normal")
            else:
                c.itemconfigure(it[key], state="hidden")
