"""Ember design tokens — the whole visual system, in one screen.

Every value the HUD draws traces back to something in this file. That is the
entire point: nearly every ugly interface is ugly because nobody chose the
values up front, so each surface invented its own. These used to live as loose
literals scattered through overlay.py's render code (an orb at x+34, text at
x+62, a 16px nudge here, a 20px one there), which is how a layout drifts.

Two halves:

  * Module constants — colour, timing, and the audio-reactive curve. These are
    resolution-independent and shared with the headless ``OverlayModel``.
  * ``Layout`` — every pixel measurement, built once per window against the
    real display DPI. Windows desktop UI's single most common
    "looks broken on someone else's machine" bug is assuming 96 DPI, so the
    numbers below are design units at 96 DPI and Layout scales them.
"""
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Colour — "Ember": a warm, muted family (cooling ash -> glowing coal) instead
# of a stock UI-kit swatch per state. One coherent value/chroma curve so every
# accent still reads as part of the same object.
#
# Contrast against PANEL, measured (WCAG 2.1): primary 16.2, secondary 6.9,
# transcript 13.7, and every rendered accent >= 5.3. All clear 4.5 body text.
# SLEEPING is 3.5 and is the one exception, which costs nothing because
# SLEEPING is never drawn -- an idle HUD is invisible by design.
# ---------------------------------------------------------------------------
PANEL = "#15120E"
TEXT_PRIMARY = "#F4EEE4"
TEXT_SECONDARY = "#A79C8C"
TEXT_TRANSCRIPT = "#E6DCC8"
SHADOW = "#0A0705"       # crisp offset drop-shadow sliver (no blur on Canvas)
KEY = "#010203"          # transparent-color key, never used in the palette

# Per-state accent — one hue each, never reused.
ACCENTS = {
    "SLEEPING": "#726A5C",   # cooling ash — never rendered (SLEEPING is invisible)
    "WOKE": "#F4EEE4",       # near-white spark — deliberately not a hue: "heard you"
    "LISTENING": "#3FD1C0",  # bright teal — ears open
    "THINKING": "#9B80E8",   # amethyst — turning it over
    "SPEAKING": "#E8A83D",   # warm gold — Nok has the floor
    "ACTING": "#5BC98A",     # jade — doing it
    "TEACHING": "#E8A83D",   # retired with teach mode; kept so old events render
    "ERROR": "#E1604A",      # glowing coal-red — something's wrong
    "DONE": "#5BC98A",       # same hue as ACTING — landed cleanly
    "NEUTRAL": "#8C8175",
}

# ---------------------------------------------------------------------------
# Type — four sizes off a 11pt base on a 1.25 ratio, two weights. There were
# five ad-hoc sizes (10, 11, 13, 14, 15) with no relationship to each other.
# ---------------------------------------------------------------------------
FONT_FAMILY = "Segoe UI"
FONT_MONO = ("Cascadia Mono", "Consolas")   # preference order

TYPE_BASE = 11
TYPE_RATIO = 1.25
TYPE_SM = 9      # round(11 / 1.25)  — labels, secondary lines
TYPE_MD = 11     # base              — mono timer, chips
TYPE_LG = 14     # round(11 * 1.25)  — glyphs, transcript
TYPE_XL = 17     # round(11 * 1.25^2) — the title, the one thing read first

# ---------------------------------------------------------------------------
# Space — 4px base unit. Every measurement below is a multiple of it. If a
# value ever needs to sit off this scale, the layout is wrong, not the scale.
# ---------------------------------------------------------------------------
SPACE = 4

# ---------------------------------------------------------------------------
# Motion — two durations and one shape. Things entering decelerate.
# ---------------------------------------------------------------------------
MOTION_FAST = 0.12     # state feedback
MOTION_SLOW = 0.28     # entrances

SIZE_EASE = 0.18       # size change eases over ~180ms
WOKE_HOLD = 0.22       # the "I heard you" spark before LISTENING takes over
DONE_HOLD = 1.8        # DONE resolve holds before fading to SLEEPING
ERROR_STICKY = 4.0     # hold the error visual this long since ERROR entered
ERROR_MAX_LINK = 6.0   # only treat SLEEPING as an error-release if within this
SLEEP_DIM_AFTER = 6.0  # SLEEPING dims alpha after this many idle seconds

# -- audio-reactive level (see docs/design-review-v2.md §5) -----------------
# Constants borrowed from clicky_windows' waveform driver, a sibling product
# that solved the same problem against real mic input.
NOISE_FLOOR = 0.008    # below this, treat as silence
LEVEL_GAIN = 2.85      # raw RMS rarely exceeds ~0.35; this maps that to ~1.0
LEVEL_CURVE = 0.76     # <1 compresses perceptually: quiet speech still moves
TAU_ATTACK = 0.05      # ~50ms rise — reads as instant
TAU_DECAY = 0.25       # ~250ms fall — no flicker between syllables

RING_ONSET = 0.15      # level crossing that spawns a new sonar ring
RING_MIN_GAP = 0.25    # never spawn rings faster than this
RING_LIFE_IN = 0.40    # LISTENING rings collapse inward over this
RING_LIFE_OUT = 0.55   # SPEAKING rings expand outward, slightly slower


@dataclass(frozen=True)
class Layout:
    """Every pixel the HUD draws, resolved against one display's DPI.

    Build it with ``Layout.at(scale)``. All the defaults are design units at
    96 DPI; at 150% Windows scaling the same HUD is built at 1.5x so it stays
    the same physical size on the glass instead of shrinking to illegibility.
    """

    scale: float = 1.0

    # card
    compact_w: int = 380
    compact_h: int = 64
    expanded_w: int = 560
    expanded_h: int = 104
    radius: int = 24          # near-full pill at compact height, soft card expanded
    margin_x: int = 8         # window is slightly larger than the card so the
    margin_y: int = 16        # shadow and the shake never clip at the edges
    bottom_gap: int = 96      # sits above the taskbar, clear of the content

    # card internals
    shadow_offset: int = 3
    orb_x: int = 32           # 8 * SPACE
    orb_radius: float = 12.0  # 3 * SPACE
    halo_1: int = 4
    halo_2: int = 8
    halo_3: int = 12
    text_x: int = 64          # 16 * SPACE — clears the orb and its halo
    line_gap: int = 16        # 4 * SPACE — title/subtitle split about centre
    timer_inset: int = 20
    chip_pad_x: int = 8
    chip_pad_y: int = 4
    stroke: int = 3

    @classmethod
    def at(cls, scale: float) -> "Layout":
        """Scale every measurement to a display. 1.0 is 96 DPI."""
        scale = max(1.0, float(scale))
        if scale == 1.0:
            return cls()

        def s(v):
            return type(v)(round(v * scale)) if isinstance(v, int) else v * scale

        return cls(
            scale=scale,
            compact_w=s(cls.compact_w), compact_h=s(cls.compact_h),
            expanded_w=s(cls.expanded_w), expanded_h=s(cls.expanded_h),
            radius=s(cls.radius),
            margin_x=s(cls.margin_x), margin_y=s(cls.margin_y),
            bottom_gap=s(cls.bottom_gap),
            shadow_offset=s(cls.shadow_offset),
            orb_x=s(cls.orb_x), orb_radius=cls.orb_radius * scale,
            halo_1=s(cls.halo_1), halo_2=s(cls.halo_2), halo_3=s(cls.halo_3),
            text_x=s(cls.text_x), line_gap=s(cls.line_gap),
            timer_inset=s(cls.timer_inset),
            chip_pad_x=s(cls.chip_pad_x), chip_pad_y=s(cls.chip_pad_y),
            stroke=s(cls.stroke),
        )

    # -- type, scaled to match ------------------------------------------
    #
    # tkinter takes a NEGATIVE font size to mean "pixels" rather than points.
    # Points would be re-scaled by Tk's own DPI factor on top of ours and the
    # text would outgrow the card it sits in; pixels keep type and layout
    # locked to the same multiplier.

    def _font(self, size, *style):
        return (FONT_FAMILY, -int(round(size * self.scale)), *style)

    @property
    def font_title(self):
        return self._font(TYPE_XL, "bold")

    @property
    def font_label(self):
        return self._font(TYPE_SM, "bold")

    @property
    def font_transcript(self):
        return self._font(TYPE_LG, "italic")

    @property
    def font_glyph(self):
        return self._font(TYPE_LG, "bold")

    def font_mono(self, family):
        return (family, -int(round(TYPE_MD * self.scale)))

    def dims(self, expanded):
        return ((self.expanded_w, self.expanded_h) if expanded
                else (self.compact_w, self.compact_h))
