"""Which side of the chassis did the knock land on?

Design constraint: the tested mono path (RingBuffer -> KnockDetector) must not
change. So side detection runs *alongside* it on a separate, tiny buffer.

The capture callback writes two things per block:
  * mono (mean of channels) -> RingBuffer, for the existing classifier
  * per-channel peak amplitudes -> SideBuffer, for side lookup

At onset time the detector asks SideBuffer for the side of the block that just
fired. Cost is one small array write per block and a dict lookup per onset --
nothing that threatens the 32ms budget.

Side is decided by the inter-channel level difference (ILD) in dB between the
two channels furthest apart in the array. `bias_db` recentres the decision
because laptop mic arrays are rarely symmetric about the chassis midline; it
comes from calibration.

If the array is beamformed to the point where ILD carries no spatial
information, `calibrate_sides` will report a low d' and the caller should turn
side detection off rather than ship a coin flip.
"""
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

LEFT = "LEFT"
RIGHT = "RIGHT"
UNKNOWN = "UNKNOWN"

# d' below this means the two sides are not reliably distinguishable.
MIN_SEPARATION_DPRIME = 2.0


@dataclass
class SideConfig:
    """Which channels to compare, and where the decision boundary sits."""

    left_ch: int = 0
    right_ch: int = 1
    bias_db: float = 0.0        # ILD value that means "dead centre"
    deadzone_db: float = 0.0    # |ild - bias| below this -> UNKNOWN
    enabled: bool = False       # off until calibration proves separability

    @classmethod
    def from_dict(cls, d: dict) -> "SideConfig":
        if not d:
            return cls()
        return cls(
            left_ch=int(d.get("left_ch", 0)),
            right_ch=int(d.get("right_ch", 1)),
            bias_db=float(d.get("bias_db", 0.0)),
            deadzone_db=float(d.get("deadzone_db", 0.0)),
            enabled=bool(d.get("enabled", False)),
        )

    def to_dict(self) -> dict:
        return {
            "left_ch": self.left_ch,
            "right_ch": self.right_ch,
            "bias_db": round(self.bias_db, 4),
            "deadzone_db": round(self.deadzone_db, 4),
            "enabled": self.enabled,
        }


def ild_db(peaks, left_ch: int, right_ch: int) -> float:
    """Inter-channel level difference in dB. Positive => louder on left_ch."""
    peaks = np.asarray(peaks, dtype=np.float64).reshape(-1)
    if peaks.size <= max(left_ch, right_ch):
        return 0.0
    lo = float(peaks[left_ch]) + 1e-9
    ro = float(peaks[right_ch]) + 1e-9
    return float(20.0 * np.log10(lo / ro))


class SideBuffer:
    """Ring of recent per-block channel peaks, timestamped by sample index.

    Written from the audio callback (must stay cheap), read from the detector
    thread. A deque of fixed maxlen is atomic enough for this access pattern:
    the reader only ever scans a snapshot and tolerates a missed tail block.
    """

    def __init__(self, maxlen: int = 256):
        self._buf = deque(maxlen=maxlen)

    def write(self, t: float, peaks) -> None:
        # np.max over a (blocksize, channels) frame -> one peak per channel.
        self._buf.append((t, np.asarray(peaks, dtype=np.float32)))

    def peaks_at(self, t: float, tolerance: float = 0.05):
        """Peaks of the block nearest `t`, or None if nothing is close enough."""
        best, best_dt = None, tolerance
        for ts, peaks in list(self._buf):
            dt = abs(ts - t)
            if dt <= best_dt:
                best, best_dt = peaks, dt
        return best

    def __len__(self) -> int:
        return len(self._buf)


class SideClassifier:
    """Turns an onset timestamp into LEFT / RIGHT / UNKNOWN."""

    def __init__(self, buffer: SideBuffer, config: Optional[SideConfig] = None):
        self.buffer = buffer
        self.config = config or SideConfig()

    def side_at(self, t: float) -> str:
        cfg = self.config
        if not cfg.enabled:
            return UNKNOWN
        peaks = self.buffer.peaks_at(t)
        if peaks is None:
            return UNKNOWN
        delta = ild_db(peaks, cfg.left_ch, cfg.right_ch) - cfg.bias_db
        if abs(delta) < cfg.deadzone_db:
            return UNKNOWN
        return LEFT if delta > 0 else RIGHT


def majority_side(sides) -> str:
    """Collapse a chain's per-onset sides into one verdict.

    A chain is one physical gesture, so a single confident onset is enough;
    UNKNOWNs are abstentions, not votes. Ties -> UNKNOWN (never guess).
    """
    votes = [s for s in sides if s in (LEFT, RIGHT)]
    if not votes:
        return UNKNOWN
    n_left = votes.count(LEFT)
    n_right = votes.count(RIGHT)
    if n_left == n_right:
        return UNKNOWN
    return LEFT if n_left > n_right else RIGHT


def calibrate_sides(left_samples, right_samples, left_ch=0, right_ch=1) -> dict:
    """Derive a SideConfig from labelled knock peaks.

    `left_samples` / `right_samples` are sequences of per-channel peak arrays.
    Returns a dict with the fitted config plus the separability evidence, so
    the caller can refuse to enable a coin flip.
    """
    lo = np.array([ild_db(p, left_ch, right_ch) for p in left_samples])
    ro = np.array([ild_db(p, left_ch, right_ch) for p in right_samples])
    if lo.size == 0 or ro.size == 0:
        return {"enabled": False, "reason": "no samples", "d_prime": 0.0}

    # Boundary midway between the class means; spread sets the dead zone.
    bias = float((lo.mean() + ro.mean()) / 2.0)
    pooled_sd = float((lo.std() + ro.std()) / 2.0)
    separation = float(abs(lo.mean() - ro.mean()))
    d_prime = separation / (pooled_sd + 1e-9)

    # Orientation: whichever class sits above the boundary is "left_ch louder".
    # If the labelled LEFT knocks came out quieter on left_ch, the channels are
    # swapped relative to the physical chassis -- fix it here, not at read time.
    swapped = lo.mean() < ro.mean()
    if swapped:
        left_ch, right_ch = right_ch, left_ch
        bias = -bias

    enabled = d_prime >= MIN_SEPARATION_DPRIME
    return {
        "left_ch": left_ch,
        "right_ch": right_ch,
        "bias_db": bias,
        "deadzone_db": round(pooled_sd * 0.5, 4),
        "enabled": enabled,
        "d_prime": round(d_prime, 3),
        "separation_db": round(separation, 3),
        "pooled_sd_db": round(pooled_sd, 3),
        "n_left": int(lo.size),
        "n_right": int(ro.size),
        "reason": "" if enabled else
                  f"d'={d_prime:.2f} < {MIN_SEPARATION_DPRIME} (array is beamformed/downmixed)",
    }
