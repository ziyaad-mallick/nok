"""Inter-onset-interval (IOI) state machine for tap-pattern recognition.

Pure module: no audio / numpy / I/O dependencies. Feed it onset timestamps
(seconds, monotonic) via `feed`, and poll it with the current time via `poll`;
it emits DOUBLE_TAP / TRIPLE_TAP / NONE decisions.

State machine
-------------

    +--------+  onset            +----------------------+
    |  IDLE  |------------------>|       CHAINING        |
    | (empty)|  start chain      |  (1..MAX onsets held) |
    +--------+                   +----------+-----------+
        ^                                   |
        |                                   |  feed(onset):
        |                                   |    IOI < MIN_IOI  -> debounce-drop (bounce/jitter)
        |                                   |    MIN <= IOI <= MAX -> extend chain
        |                                   |    IOI > MAX_IOI  -> finalize this chain, restart on new onset
        |                                   |    chain len > MAX -> POISONED (storm) -> will finalize as NONE
        |  poll(now):                       |
        +-----------------------------------+
           now - last_onset > CLOSE_OUT  -> finalize + emit decision, back to IDLE

Finalize classifies the held chain by count + IOIs:
  * 2 onsets, IOI in [MIN_IOI, MAX_IOI]                    -> DOUBLE_TAP
  * 3 onsets, both consecutive IOIs in [MIN_IOI, MAX_IOI]  -> TRIPLE_TAP
  * anything else (single tap, poisoned storm, bad spacing) -> NONE

CLOSE_OUT (> MAX_IOI) is the grace window after the last tap before a decision
is emitted, so a double can still grow into a triple. `_classify` receives the
full IOI vector, so a signature/rhythm matcher (e.g. shave-and-a-haircut) can
be slotted in later by matching the vector against templates before falling
back to the count-based rules -- no restructuring required.
"""
from typing import List, Optional

from .side import UNKNOWN, majority_side

DOUBLE_TAP = "DOUBLE_TAP"
TRIPLE_TAP = "TRIPLE_TAP"
NONE = "NONE"


class Decision(str):
    """A pattern verdict that also remembers which side the chain landed on.

    Subclasses `str` so every existing `decision == DOUBLE_TAP` comparison,
    log line and dict key keeps working untouched; `.side` is additive.
    """

    __slots__ = ("side",)

    def __new__(cls, pattern: str, side: str = UNKNOWN):
        obj = super().__new__(cls, pattern)
        obj.side = side
        return obj

# Timing windows (seconds). Tunable per-machine via the constructor.
MIN_IOI = 0.120   # faster than this between onsets = mechanical bounce, ignore
MAX_IOI = 0.450   # slower than this = the chain is broken (separate taps)
CLOSE_OUT = 0.600 # silence after last tap before the chain is finalized

MAX_PATTERN_LEN = 3  # longest recognized pattern (triple); longer = storm -> NONE


class IOIStateMachine:
    """Recognizes double/triple taps from a stream of onset timestamps."""

    def __init__(
        self,
        min_ioi: float = MIN_IOI,
        max_ioi: float = MAX_IOI,
        close_out: float = CLOSE_OUT,
    ):
        if not (0 < min_ioi < max_ioi < close_out):
            raise ValueError("require 0 < min_ioi < max_ioi < close_out")
        self.min_ioi = min_ioi
        self.max_ioi = max_ioi
        self.close_out = close_out
        self._chain: List[float] = []
        self._sides: List[str] = []
        self._poisoned = False

    def reset(self) -> None:
        self._chain = []
        self._sides = []
        self._poisoned = False

    def feed(self, ts: float, side: str = UNKNOWN) -> Optional[str]:
        """Register an onset at time `ts`.

        Returns a decision string ONLY when this onset finalizes a *previous*
        chain (i.e. it arrived too late to belong to it); otherwise None. The
        common double/triple case is finalized by `poll`, not here.
        """
        if not self._chain:
            self._chain = [ts]
            self._sides = [side]
            self._poisoned = False
            return None

        ioi = ts - self._chain[-1]

        if ioi < self.min_ioi:
            # Same physical tap ringing twice (bounce/jitter) -> ignore.
            return None

        if ioi > self.max_ioi:
            # Too slow to chain: the held chain is done. Decide it, then start
            # a fresh chain from this onset.
            decision = self._finalize()
            self._chain = [ts]
            self._sides = [side]
            self._poisoned = False
            return decision

        # Valid chaining interval.
        if self._poisoned or len(self._chain) >= MAX_PATTERN_LEN:
            # Storm: keep only the latest timestamp so close-out timing still
            # works, but the chain is doomed to classify as NONE.
            self._poisoned = True
            self._chain[-1] = ts
            self._sides[-1] = side
        else:
            self._chain.append(ts)
            self._sides.append(side)
        return None

    def poll(self, now: float) -> Optional[str]:
        """Call periodically with the current time.

        Returns a decision when the held chain has closed out (no new onset
        within `close_out` of the last one); otherwise None.
        """
        if not self._chain:
            return None
        if now - self._chain[-1] > self.close_out:
            decision = self._finalize()
            self._chain = []
            self._sides = []
            self._poisoned = False
            return decision
        return None

    # -- internal --------------------------------------------------------

    def _finalize(self) -> str:
        side = majority_side(self._sides)
        if self._poisoned:
            return Decision(NONE, side)
        return Decision(self._classify(self._chain), side)

    def _classify(self, chain: List[float]) -> str:
        """Map a closed chain to a decision.

        Extension point: `iois` is the full inter-onset-interval vector; a
        future rhythm/signature matcher would test it against templates here
        before the count-based fallback below.
        """
        n = len(chain)
        iois = [chain[i + 1] - chain[i] for i in range(n - 1)]
        if all(self.min_ioi <= d <= self.max_ioi for d in iois):
            if n == 2:
                return DOUBLE_TAP
            if n == 3:
                return TRIPLE_TAP
        return NONE
