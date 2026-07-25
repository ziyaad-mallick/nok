import numpy as np
import pytest

from nok.side import (
    LEFT,
    RIGHT,
    UNKNOWN,
    SideBuffer,
    SideClassifier,
    SideConfig,
    calibrate_sides,
    ild_db,
    majority_side,
)


# -- ild_db ---------------------------------------------------------------

def test_ild_zero_when_channels_equal():
    assert ild_db([0.5, 0.5], 0, 1) == pytest.approx(0.0)


def test_ild_positive_when_left_channel_louder():
    assert ild_db([1.0, 0.1], 0, 1) == pytest.approx(20.0, abs=0.01)


def test_ild_negative_when_right_channel_louder():
    assert ild_db([0.1, 1.0], 0, 1) == pytest.approx(-20.0, abs=0.01)


def test_ild_survives_silent_channel_without_dividing_by_zero():
    assert ild_db([0.0, 0.0], 0, 1) == pytest.approx(0.0)
    assert np.isfinite(ild_db([1.0, 0.0], 0, 1))


def test_ild_returns_zero_when_channel_index_out_of_range():
    assert ild_db([0.5], 0, 1) == 0.0


# -- SideBuffer -----------------------------------------------------------

def test_buffer_returns_peaks_for_nearby_timestamp():
    buf = SideBuffer()
    buf.write(1.00, [0.9, 0.1])
    assert buf.peaks_at(1.01) is not None


def test_buffer_returns_none_when_nothing_within_tolerance():
    buf = SideBuffer()
    buf.write(1.0, [0.9, 0.1])
    assert buf.peaks_at(5.0) is None


def test_buffer_picks_the_closest_block():
    buf = SideBuffer()
    buf.write(1.00, [1.0, 0.0])
    buf.write(1.04, [0.0, 1.0])
    got = buf.peaks_at(1.039)
    assert got[1] == pytest.approx(1.0)


def test_buffer_is_bounded():
    buf = SideBuffer(maxlen=4)
    for i in range(50):
        buf.write(float(i), [0.5, 0.5])
    assert len(buf) == 4


# -- SideClassifier -------------------------------------------------------

def _clf(**kw):
    buf = SideBuffer()
    cfg = SideConfig(enabled=True, **kw)
    return buf, SideClassifier(buf, cfg)


def test_classifier_is_unknown_while_disabled():
    buf = SideBuffer()
    buf.write(1.0, [1.0, 0.01])
    clf = SideClassifier(buf, SideConfig(enabled=False))
    assert clf.side_at(1.0) == UNKNOWN


def test_classifier_reads_left_and_right():
    buf, clf = _clf()
    buf.write(1.0, [1.0, 0.1])
    buf.write(2.0, [0.1, 1.0])
    assert clf.side_at(1.0) == LEFT
    assert clf.side_at(2.0) == RIGHT


def test_classifier_is_unknown_with_no_matching_block():
    _, clf = _clf()
    assert clf.side_at(9.0) == UNKNOWN


def test_deadzone_abstains_near_the_boundary():
    buf, clf = _clf(deadzone_db=6.0)
    buf.write(1.0, [1.0, 0.9])  # ~0.9 dB, well inside the dead zone
    assert clf.side_at(1.0) == UNKNOWN


def test_bias_shifts_the_boundary():
    """An array skewed +12dB toward ch0 should still read RIGHT below its bias."""
    buf, clf = _clf(bias_db=12.0)
    buf.write(1.0, [1.0, 0.5])  # ~6 dB, under the 12 dB bias
    assert clf.side_at(1.0) == RIGHT


# -- majority_side --------------------------------------------------------

def test_majority_ignores_unknown_abstentions():
    assert majority_side([LEFT, UNKNOWN, UNKNOWN]) == LEFT


def test_majority_wins_the_vote():
    assert majority_side([LEFT, RIGHT, LEFT]) == LEFT


def test_majority_refuses_to_guess_on_a_tie():
    assert majority_side([LEFT, RIGHT]) == UNKNOWN


def test_majority_of_nothing_is_unknown():
    assert majority_side([]) == UNKNOWN
    assert majority_side([UNKNOWN, UNKNOWN]) == UNKNOWN


# -- calibrate_sides ------------------------------------------------------

def _samples(mean_db, n=10, spread=0.2, seed=0):
    """Peak pairs whose ILD centres on `mean_db`."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        db = mean_db + rng.normal(0, spread)
        out.append([10 ** (db / 20.0), 1.0])
    return out


def test_calibration_enables_on_clean_separation():
    got = calibrate_sides(_samples(+12.0), _samples(-12.0, seed=1))
    assert got["enabled"] is True
    assert got["d_prime"] > 2.0


def test_calibration_refuses_when_sides_overlap():
    """A beamformed array: both sides produce the same ILD."""
    got = calibrate_sides(_samples(0.0, spread=3.0), _samples(0.0, spread=3.0, seed=1))
    assert got["enabled"] is False
    assert "beamformed" in got["reason"]


def test_calibration_bias_lands_between_the_classes():
    got = calibrate_sides(_samples(+20.0), _samples(+4.0, seed=1))
    assert got["bias_db"] == pytest.approx(12.0, abs=1.0)


def test_calibration_autocorrects_swapped_channels():
    """Physically-left knocks arriving louder on ch1 must swap, not invert."""
    got = calibrate_sides(_samples(-12.0), _samples(+12.0, seed=1))
    assert got["enabled"] is True
    assert (got["left_ch"], got["right_ch"]) == (1, 0)


def test_calibrated_config_round_trips_and_classifies_correctly():
    got = calibrate_sides(_samples(+12.0), _samples(-12.0, seed=1))
    cfg = SideConfig.from_dict(got)
    buf = SideBuffer()
    clf = SideClassifier(buf, cfg)
    buf.write(1.0, [10 ** (12 / 20.0), 1.0])
    buf.write(2.0, [10 ** (-12 / 20.0), 1.0])
    assert clf.side_at(1.0) == LEFT
    assert clf.side_at(2.0) == RIGHT


def test_calibration_with_no_samples_is_disabled():
    assert calibrate_sides([], [])["enabled"] is False
