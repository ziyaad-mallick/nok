"""Timing-vector tests for the IOI state machine.

Onsets are fed as plain float timestamps (seconds); `poll` is called with a
time far past the last onset to force close-out and collect the decision.
"""
from nok.ioi import (
    CLOSE_OUT,
    DOUBLE_TAP,
    MAX_IOI,
    MIN_IOI,
    NONE,
    TRIPLE_TAP,
    IOIStateMachine,
)


def _decisions(onsets, flush=True):
    """Feed all onsets, then flush; return the list of non-None decisions."""
    m = IOIStateMachine()
    out = []
    for ts in onsets:
        d = m.feed(ts)
        if d is not None:
            out.append(d)
    if flush and onsets:
        d = m.poll(onsets[-1] + CLOSE_OUT + 1.0)
        if d is not None:
            out.append(d)
    return out


def test_valid_double_tap():
    assert _decisions([0.0, 0.2]) == [DOUBLE_TAP]


def test_valid_triple_tap():
    assert _decisions([0.0, 0.2, 0.4]) == [TRIPLE_TAP]


def test_too_fast_jitter_is_not_a_double():
    # 50ms apart -> mechanical bounce, debounced -> single tap -> NONE
    assert _decisions([0.0, 0.05]) == [NONE]


def test_jitter_then_valid_partner_still_double():
    # bounce is dropped, the real second tap lands in the valid window
    assert _decisions([0.0, 0.05, 0.25]) == [DOUBLE_TAP]


def test_too_slow_is_two_singles():
    # 1s apart -> chain broken -> two separate NONE decisions
    assert _decisions([0.0, 1.0]) == [NONE, NONE]


def test_typing_burst_storm_valid_spacing():
    # many onsets at valid IOI -> chain overflows -> poisoned -> NONE
    onsets = [i * 0.2 for i in range(12)]
    assert _decisions(onsets) == [NONE]


def test_typing_burst_storm_ultrafast():
    # a dense flutter (all sub-min IOI) collapses to one onset -> NONE
    onsets = [i * 0.03 for i in range(20)]
    assert _decisions(onsets) == [NONE]


def test_boundary_min_ioi_inclusive_double():
    assert _decisions([0.0, MIN_IOI]) == [DOUBLE_TAP]


def test_boundary_max_ioi_inclusive_double():
    assert _decisions([0.0, MAX_IOI]) == [DOUBLE_TAP]


def test_boundary_just_over_max_is_two_singles():
    assert _decisions([0.0, MAX_IOI + 0.01]) == [NONE, NONE]


def test_poll_does_not_fire_before_close_out():
    m = IOIStateMachine()
    m.feed(0.0)
    m.feed(0.2)
    # still inside the close-out grace window -> no decision yet
    assert m.poll(0.2 + CLOSE_OUT - 0.01) is None
    # past it -> the double resolves
    assert m.poll(0.2 + CLOSE_OUT + 0.01) == DOUBLE_TAP


def test_feed_finalizes_previous_chain_on_late_onset():
    m = IOIStateMachine()
    m.feed(0.0)
    assert m.feed(0.2) is None                 # pending double
    # a much later onset closes out the double and starts a new chain
    assert m.feed(5.0) == DOUBLE_TAP


def test_reset_clears_pending_chain():
    m = IOIStateMachine()
    m.feed(0.0)
    m.feed(0.2)
    m.reset()
    assert m.poll(100.0) is None


def test_two_separate_doubles():
    onsets = [0.0, 0.2, 2.0, 2.2]
    assert _decisions(onsets) == [DOUBLE_TAP, DOUBLE_TAP]


def test_constructor_rejects_bad_windows():
    import pytest
    with pytest.raises(ValueError):
        IOIStateMachine(min_ioi=0.5, max_ioi=0.4, close_out=0.6)
    with pytest.raises(ValueError):
        IOIStateMachine(min_ioi=0.1, max_ioi=0.7, close_out=CLOSE_OUT)
