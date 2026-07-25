"""Design tokens and DPI-aware layout.

Pure maths, no tkinter, no window. The point of these is that the HUD is the
same physical size on a 150%-scaled laptop as on a 96 DPI monitor -- assuming
96 DPI is the classic reason Windows desktop UI looks broken on someone else's
machine.
"""
import pytest

from nok import tokens
from nok.tokens import Layout


def test_default_layout_is_unscaled():
    lay = Layout()
    assert lay.scale == 1.0
    assert (lay.compact_w, lay.compact_h) == (380, 64)


def test_layout_scales_every_measurement_together():
    base, big = Layout(), Layout.at(1.5)

    assert big.compact_w == round(base.compact_w * 1.5)
    assert big.expanded_h == round(base.expanded_h * 1.5)
    assert big.radius == round(base.radius * 1.5)
    assert big.text_x == round(base.text_x * 1.5)
    assert big.orb_radius == pytest.approx(base.orb_radius * 1.5)


def test_type_scales_with_the_layout():
    """Type and box must move by the same factor or the text outgrows the card."""
    base, big = Layout(), Layout.at(2.0)

    # tkinter reads a negative size as pixels, which is what keeps Tk's own
    # DPI factor from multiplying on top of ours.
    assert base.font_title[1] < 0
    assert big.font_title[1] == base.font_title[1] * 2


def test_layout_never_scales_below_one():
    """A reported sub-96 DPI must not shrink the HUD into unreadability."""
    assert Layout.at(0.5).scale == 1.0
    assert Layout.at(0.5).compact_w == Layout().compact_w


def test_dims_switches_on_expanded():
    lay = Layout()
    assert lay.dims(False) == (lay.compact_w, lay.compact_h)
    assert lay.dims(True) == (lay.expanded_w, lay.expanded_h)


def test_type_scale_is_derived_not_arbitrary():
    """Four sizes off one base and one ratio, ascending."""
    sizes = [tokens.TYPE_SM, tokens.TYPE_MD, tokens.TYPE_LG, tokens.TYPE_XL]
    assert sizes == sorted(sizes)
    assert len(set(sizes)) == 4
    assert tokens.TYPE_MD == tokens.TYPE_BASE
    assert tokens.TYPE_LG == round(tokens.TYPE_BASE * tokens.TYPE_RATIO)


def test_spacing_values_sit_on_the_scale():
    """Every layout measurement is a multiple of the 4px base unit."""
    lay = Layout()
    on_scale = [
        lay.orb_x, lay.text_x, lay.line_gap, lay.halo_1, lay.halo_2,
        lay.halo_3, lay.chip_pad_x, lay.chip_pad_y, lay.margin_x,
        lay.margin_y, lay.timer_inset, lay.radius, lay.bottom_gap,
    ]
    off_scale = [v for v in on_scale if v % tokens.SPACE]
    assert off_scale == []


def test_every_rendered_accent_clears_body_text_contrast():
    """SLEEPING is exempt: an idle HUD is never drawn at all."""
    def _lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def _lum(h):
        h = h.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    def _ratio(a, b):
        la, lb = _lum(a), _lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    for name, accent in tokens.ACCENTS.items():
        if name == "SLEEPING":
            continue
        assert _ratio(accent, tokens.PANEL) >= 4.5, f"{name} fails body contrast"

    for text in (tokens.TEXT_PRIMARY, tokens.TEXT_SECONDARY, tokens.TEXT_TRANSCRIPT):
        assert _ratio(text, tokens.PANEL) >= 4.5
