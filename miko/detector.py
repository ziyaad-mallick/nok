"""Streaming knock detector: 512-sample float32 blocks @16kHz -> onsets.

Runs on its own consumer thread (see `KnockDetector.run`), NOT in the
sounddevice callback. Per-block features are numpy-only and cheap so the hot
path stays well under the 32ms/block budget (Amendment 9); librosa is never
imported here -- it is offline-only (calibration + fixture analysis).

Per-block pipeline
------------------

  block(512) --> [ peak = max|x| ]----------------------+
             |                                           |
             +-> [ energy = mean(x^2) ] --> EMA (trailing avg)
             |            |                     |
             |            +--> attack_ratio = energy / trailing_ema
             |                                           |
             +-> [ spectral centroid via rfft ]          |
                                                         v
   knock  ==  peak >= amplitude_floor              (loud enough)
          AND attack_ratio >= attack_ratio_thr     (sharp attack / transient)
          AND centroid <= centroid_max             (broadband LOW-freq thump)

  SIMPLE thump mode (OV-1) bypasses attack+centroid: onset == peak >= simple_amplitude.

  onset -> refractory guard -> gating guard -> emit
    gating: playback OR busy gate active, or within `cooldown_s` after either
    was last active, drops the onset (no self-waking during chime/TTS/BUSY).

Only the numpy feature step is timed (perf_counter) and rolled into
`max_block_seconds`. Logging (onset / gated_drop / wake) happens from the
consumer thread outside that timed region and only on the rare event blocks.
"""
import json
import time
from dataclasses import dataclass
from time import perf_counter
from typing import Optional

import numpy as np

from .ioi import DOUBLE_TAP, TRIPLE_TAP
from .logs import log_event
from .ring_buffer import SAMPLE_RATE
from .side import UNKNOWN

BLOCKSIZE = 512  # mirrors miko.audio.BLOCKSIZE (not imported to stay sounddevice-free)


def spectral_centroid(block, sr, freqs=None, window=None):
    """Magnitude-weighted mean frequency (Hz) of one block.

    `freqs` (np.fft.rfftfreq) and `window` may be passed pre-computed to avoid
    per-call allocation on the hot path; offline callers omit them.
    """
    block = np.asarray(block, dtype=np.float32)
    if block.size == 0:
        return 0.0
    x = block if window is None else block * window
    mag = np.abs(np.fft.rfft(x))
    total = float(mag.sum())
    if total <= 0.0:
        return 0.0
    if freqs is None:
        freqs = np.fft.rfftfreq(block.size, 1.0 / sr)
    return float(np.dot(freqs, mag) / total)


@dataclass
class DetectorConfig:
    samplerate: int = SAMPLE_RATE
    blocksize: int = BLOCKSIZE
    # classifier thresholds (overridden by calibration.json in production)
    attack_ratio: float = 3.0       # energy jump vs trailing average
    centroid_max: float = 2200.0    # Hz; knocks sit below this, typing above
    amplitude_floor: float = 0.05   # peak |x| gate (float32 audio in [-1, 1])
    # feature dynamics
    ema_alpha: float = 0.15         # trailing-average smoothing per block
    warmup_blocks: int = 3          # prime the EMA before emitting onsets
    refractory_s: float = 0.08      # min spacing between emitted onset candidates
    cooldown_s: float = 0.5         # gate cooldown after chime/TTS/BUSY (OV-4)
    # SIMPLE thump mode (OV-1): any single loud spike wakes, classifier bypassed.
    # Must sit BELOW a typical calibrated amplitude_floor so --thump is a genuine
    # last-resort fallback (looser), not stricter than normal mode. Override with
    # MIKO_THUMP_AMPLITUDE when the mic runs quiet.
    simple_mode: bool = False
    simple_amplitude: float = 0.08

    @classmethod
    def from_calibration(cls, data: dict) -> "DetectorConfig":
        d = cls()
        if not isinstance(data, dict):
            return d
        th = data.get("thresholds")
        # A half-written calibration.json parses fine but can hold a null or a
        # list here; falling back to defaults beats crashing before startup.
        if not isinstance(th, dict):
            th = {}
        return cls(
            samplerate=int(data.get("samplerate", d.samplerate)),
            attack_ratio=float(th.get("attack_ratio", d.attack_ratio)),
            centroid_max=float(th.get("centroid_max", d.centroid_max)),
            amplitude_floor=float(th.get("amplitude_floor", d.amplitude_floor)),
            simple_mode=bool(data.get("simple_mode", d.simple_mode)),
        )

    @classmethod
    def from_calibration_file(cls, path: str) -> "DetectorConfig":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_calibration(json.load(f))


WAKE_BLOCK_DRAIN_S = 0.5  # buffer growth above this during a chunk == a wake ran


def _consumed_after_chunk(total_before: int, total_now: int, sr: int) -> int:
    """Decide the new `consumed` pointer after processing one chunk.

    A wake runs the whole listen/act pipeline synchronously on the detector
    thread; meanwhile the capture thread keeps filling the buffer with the
    chime, the spoken command, and TTS. Resuming from `total_before` would
    replay all of that through the classifier and mis-read the user's own voice
    as fresh knocks (self-retrigger). When the buffer grew by seconds we were
    blocked in a wake, so discard that backlog; otherwise advance normally.
    """
    if total_now - total_before > int(WAKE_BLOCK_DRAIN_S * sr):
        return total_now
    return total_before


class KnockDetector:
    """Stateful per-block knock classifier with playback/BUSY gating."""

    def __init__(self, config: Optional[DetectorConfig] = None, side_classifier=None):
        self.config = config or DetectorConfig()
        # Optional: turns each onset timestamp into LEFT/RIGHT. None (or a
        # classifier whose calibration failed) means every onset is UNKNOWN
        # and the gesture map falls back to side-agnostic bindings.
        self.side_classifier = side_classifier
        n = self.config.blocksize
        # Pre-compute window + rfft frequency axis once (hot-path allocation-free).
        self._window = np.hanning(n).astype(np.float32)
        self._freqs = np.fft.rfftfreq(n, 1.0 / self.config.samplerate)

        self._ema = 0.0
        self._blocks_seen = 0
        self._last_candidate_at = -1e9  # last onset candidate (emitted or dropped)
        self._last_gated_at = -1e9      # last time a gate was active

        # Set by external code (dispatcher/TTS) -- plain attributes, like audio.py.
        self.playback_gate = False
        self.busy_gate = False

        self.max_block_seconds = 0.0    # rolling max feature-compute time

    # -- external gate setters (optional sugar) --------------------------

    def set_playback_gate(self, on: bool) -> None:
        self.playback_gate = bool(on)

    def set_busy_gate(self, on: bool) -> None:
        self.busy_gate = bool(on)

    # -- per-block hot path ----------------------------------------------

    def process_block(self, block, now: float) -> Optional[float]:
        """Consume one block captured at time `now` (seconds).

        Returns `now` if a (non-gated) knock onset is emitted, else None.
        """
        block = np.asarray(block, dtype=np.float32).reshape(-1)

        # --- timed numpy feature step (the only thing under the budget) ---
        t0 = perf_counter()
        is_onset = self._classify_block(block)
        dt = perf_counter() - t0
        if dt > self.max_block_seconds:
            self.max_block_seconds = dt
        # ------------------------------------------------------------------

        self._blocks_seen += 1

        # Track gate state EVERY block so the cooldown is measured from the gate
        # RELEASE time (the last block a gate was active), not from onsets.
        gated_now = self.playback_gate or self.busy_gate
        if gated_now:
            self._last_gated_at = now

        if not is_onset:
            return None

        # Refractory: one candidate per window, so a single thump's tail (and
        # log spam during gating) cannot re-fire.
        if now - self._last_candidate_at < self.config.refractory_s:
            return None
        self._last_candidate_at = now

        # Gating: drop while a gate is active AND for cooldown_s afterwards.
        if gated_now or (now - self._last_gated_at) < self.config.cooldown_s:
            log_event(
                "gated_drop",
                t=now,
                playback=self.playback_gate,
                busy=self.busy_gate,
            )
            return None

        log_event("onset", t=now)
        return now

    def _classify_block(self, block) -> bool:
        cfg = self.config
        if block.size == 0:
            return False

        peak = float(np.max(np.abs(block)))
        energy = float(np.dot(block, block) / block.size)

        if cfg.simple_mode:
            # No classifier: a single loud spike wakes (after warmup).
            return peak >= cfg.simple_amplitude and self._blocks_seen >= cfg.warmup_blocks

        prev_ema = self._ema
        self._ema = (1.0 - cfg.ema_alpha) * self._ema + cfg.ema_alpha * energy

        if self._blocks_seen < cfg.warmup_blocks:
            return False  # let the trailing average settle first
        if peak < cfg.amplitude_floor:
            return False  # too quiet
        attack_ratio = energy / (prev_ema + 1e-9)
        if attack_ratio < cfg.attack_ratio:
            return False  # no sharp attack (sustained tone / gradual swell)
        centroid = spectral_centroid(
            block, cfg.samplerate, freqs=self._freqs, window=self._window
        )
        if centroid > cfg.centroid_max:
            return False  # high-frequency (typing click), not a low thump
        return True

    # -- consumer thread (live use; not the audio callback) --------------

    def run(self, ring_buffer, ioi, stop_event, on_wake=None, idle_sleep=0.005):
        """Loop: pull whole 512-blocks from `ring_buffer`, detect onsets, feed
        the IOI machine, and fire `on_wake(pattern, t)` on DOUBLE/TRIPLE_TAP.

        Runs until `stop_event` is set. Timestamps are sample-derived so they
        match the replay harness exactly.
        """
        sr = self.config.samplerate
        bs = self.config.blocksize
        consumed = 0
        while not stop_event.is_set():
            try:
                total = ring_buffer.total_written
                available = total - consumed
                if available < bs:
                    now = total / sr
                    self._emit(ioi.poll(now), now, on_wake)
                    time.sleep(idle_sleep)
                    continue

                n_blocks = available // bs
                want = n_blocks * bs
                chunk = ring_buffer.read_last(want / sr)
                offset = total - chunk.size  # sample index of chunk[0]
                end = chunk.size - bs + 1
                for i in range(0, end, bs):
                    block = chunk[i:i + bs]
                    now = (offset + i + bs) / sr
                    onset = self.process_block(block, now)
                    if onset is not None:
                        self._emit(ioi.feed(onset, self._side_of(onset)), now, on_wake)
                    self._emit(ioi.poll(now), now, on_wake)
                consumed = _consumed_after_chunk(total, ring_buffer.total_written, sr)
            except Exception as exc:
                # The detector thread must survive anything (a subsystem raising
                # up through on_wake, a transient buffer error) -- if it dies,
                # Miko goes silently deaf. Log and keep looping.
                log_event("detector_loop_error", error=str(exc))
                time.sleep(idle_sleep)

    def _side_of(self, onset: float) -> str:
        """Classify an onset's side, never letting that break detection."""
        if self.side_classifier is None:
            return UNKNOWN
        try:
            return self.side_classifier.side_at(onset)
        except Exception as exc:
            log_event("side_classify_error", error=str(exc))
            return UNKNOWN

    def _emit(self, decision, now, on_wake):
        if decision in (DOUBLE_TAP, TRIPLE_TAP):
            side = getattr(decision, "side", UNKNOWN)
            log_event("wake", pattern=str(decision), side=side, t=now)
            if on_wake is not None:
                on_wake(decision, now)
