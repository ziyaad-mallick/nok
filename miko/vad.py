"""Voice-activity detection + utterance endpointing.

Two interchangeable detectors behind one interface:
  - SileroVad: silero-vad ONNX on onnxruntime (Amendment 4 — no torch).
  - EnergyVad: RMS vs a rolling noise floor (the named fallback).

Both expose:
  - is_speech(frame_16k_float32) -> bool          (frame == 512 samples @16k)
  - endpoint(frames) -> np.ndarray | NO_COMMAND   (utterance capture)

get_vad() prefers silero, falls back to energy, and logs which it chose.

Endpointing state machine (endpoint()):

    WAIT_SPEECH --speech--> IN_SPEECH --800ms trailing silence--> DONE (return audio)
        |                       |
        | 5s no speech          | (speech resets the silence timer)
        v                       v
     NO_COMMAND             collect frames

The model doc: wake -> wait for speech; 5s of no speech = NO_COMMAND; after
speech starts, 800ms trailing silence = utterance complete, return the numpy
audio segment.
"""
import os
import urllib.request
from pathlib import Path

import numpy as np

from .logs import log_event
from .ring_buffer import SAMPLE_RATE

# 512 samples @ 16kHz == 32ms per frame (silero's required chunk size).
FRAME_SAMPLES = 512
FRAME_MS = FRAME_SAMPLES / SAMPLE_RATE * 1000.0

NO_SPEECH_TIMEOUT_MS = 5000.0   # wake with no usable command
TRAILING_SILENCE_MS = 800.0     # end-of-utterance
MAX_UTTERANCE_MS = 12000.0      # hard cap: never capture forever (freeze guard)

# Sentinel returned when the wake produced no speech within the timeout.
NO_COMMAND = object()

# Verified 2026-07-19: HTTP 200, 2327524 bytes, application/octet-stream.
# Pinned release tag v6.2.1; file lives at src/silero_vad/data/silero_vad.onnx.
SILERO_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    "v6.2.1/src/silero_vad/data/silero_vad.onnx"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = _REPO_ROOT / "models" / "silero_vad.onnx"


def download_silero(dest=DEFAULT_MODEL_PATH) -> Path:
    """Download the pinned silero_vad.onnx into models/ (gitignored)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log_event("silero_download_start", url=SILERO_URL, dest=str(dest))
    urllib.request.urlretrieve(SILERO_URL, dest)
    log_event("silero_download_done", dest=str(dest), bytes=dest.stat().st_size)
    return dest


class _VadBase:
    """Shared endpointing over frame iterables; subclasses implement is_speech."""

    def is_speech(self, frame) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    # Diagnostics from the most recent endpoint() call. When a listen ends in
    # NO_COMMAND, app.py logs this so a failed live wake tells us WHY (was the
    # audio silent, or was speech present but scored under threshold?).
    last_stats = {}

    def endpoint(self, frames):
        """Consume `frames` (iterable of float32 arrays) and return either the
        captured utterance (np.ndarray) or NO_COMMAND.
        """
        # Start each utterance from clean streaming state so stale recurrent
        # state from a prior wake can't suppress speech detection.
        reset = getattr(self, "reset", None)
        if callable(reset):
            reset()
        collected = []
        speech_started = False
        waited_ms = 0.0
        silence_ms = 0.0
        peak_rms = 0.0
        max_score = 0.0
        n_frames = 0

        for frame in frames:
            n_frames += 1
            arr = np.asarray(frame, dtype=np.float32)
            if arr.size:
                rms = float(np.sqrt(np.mean(arr * arr)))
                if rms > peak_rms:
                    peak_rms = rms
            speaking = self.is_speech(frame)
            score = getattr(self, "last_score", None)
            if score is not None and score > max_score:
                max_score = score
            if not speech_started:
                if speaking:
                    speech_started = True
                    silence_ms = 0.0
                    collected.append(np.asarray(frame, dtype=np.float32))
                else:
                    waited_ms += FRAME_MS
                    if waited_ms >= NO_SPEECH_TIMEOUT_MS:
                        break  # -> falls through to record stats + NO_COMMAND
            else:
                collected.append(np.asarray(frame, dtype=np.float32))
                if speaking:
                    silence_ms = 0.0
                else:
                    silence_ms += FRAME_MS
                    if silence_ms >= TRAILING_SILENCE_MS:
                        break
                # Safety valve: unbroken "speech" (music/chatter/floor pathology)
                # must never freeze Miko in LISTENING forever -> cut the utterance.
                if len(collected) * FRAME_MS >= MAX_UTTERANCE_MS:
                    break

        self.last_stats = {
            "peak_rms": round(peak_rms, 5),
            "max_speech_score": round(max_score, 4),
            "n_frames": n_frames,
            "speech_started": speech_started,
        }
        if not speech_started:
            return NO_COMMAND
        return np.concatenate(collected)


class EnergyVad(_VadBase):
    """RMS energy vs an adaptive rolling noise floor.

    A frame counts as speech when its RMS exceeds the noise floor by
    `threshold_factor`. The floor adapts only on non-speech frames so speech
    doesn't pull it up.
    """

    def __init__(self, threshold_factor=3.0, floor_alpha=0.95, floor_min=1e-4,
                 warmup_frames=4):
        self.threshold_factor = threshold_factor
        self.floor_alpha = floor_alpha
        self.floor_min = floor_min
        self.warmup_frames = warmup_frames
        self._noise_floor = None
        self._warmup = []

    def reset(self) -> None:
        """Re-seed per utterance (called by endpoint()) so a prior utterance's
        floor doesn't carry over."""
        self._noise_floor = None
        self._warmup = []

    @staticmethod
    def _rms(frame) -> float:
        frame = np.asarray(frame, dtype=np.float32)
        if frame.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(frame * frame)))

    def is_speech(self, frame) -> bool:
        rms = self._rms(frame)
        if self._noise_floor is None:
            self.last_score = 0.0
            # Seed the floor from the QUIETEST of the first few frames, not a
            # single frame: a user who starts talking the instant the chime ends
            # can't poison the floor to speech level (which would then read every
            # later frame as silence -> guaranteed NO_COMMAND).
            self._warmup.append(rms)
            if len(self._warmup) < self.warmup_frames:
                return False
            self._noise_floor = max(min(self._warmup), self.floor_min)
            self._warmup = []
            return False
        threshold = max(self._noise_floor * self.threshold_factor, self.floor_min)
        self.last_score = rms / threshold if threshold > 0 else 0.0
        speaking = rms > threshold
        if not speaking:
            self._noise_floor = (
                self.floor_alpha * self._noise_floor
                + (1.0 - self.floor_alpha) * rms
            )
        return speaking


class SileroVad(_VadBase):
    """silero-vad ONNX inference on onnxruntime (torch-free)."""

    def __init__(self, model_path=DEFAULT_MODEL_PATH, threshold=None):
        import onnxruntime as ort

        # Default lowered from 0.5 to 0.3: this machine's mic runs at low gain,
        # so real speech scored under 0.5 and every listen timed out as
        # "no_speech" (Whisper was never reached). Override with
        # MIKO_VAD_THRESHOLD=0.x without touching code.
        if threshold is None:
            threshold = float(os.environ.get("MIKO_VAD_THRESHOLD", "0.3"))
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"silero model not found at {model_path}. Run "
                "scripts/download_silero.py (or miko.vad.download_silero())."
            )
        self.threshold = threshold
        self._session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def is_speech(self, frame) -> bool:
        frame = np.asarray(frame, dtype=np.float32).reshape(-1)
        # silero needs exactly FRAME_SAMPLES; pad/truncate defensively.
        if frame.shape[0] < FRAME_SAMPLES:
            frame = np.pad(frame, (0, FRAME_SAMPLES - frame.shape[0]))
        elif frame.shape[0] > FRAME_SAMPLES:
            frame = frame[:FRAME_SAMPLES]
        inp = frame.reshape(1, FRAME_SAMPLES)
        out, self._state = self._session.run(
            None, {"input": inp, "state": self._state, "sr": self._sr}
        )
        self.last_score = float(out[0, 0])
        return self.last_score >= self.threshold


def get_vad(model_path=DEFAULT_MODEL_PATH):
    """Factory: silero if available, else energy. Logs the choice."""
    try:
        vad = SileroVad(model_path=model_path)
        log_event("vad_selected", backend="silero", model_path=str(model_path),
                  threshold=vad.threshold)
        return vad
    except Exception as exc:
        log_event("vad_selected", backend="energy", reason=str(exc))
        return EnergyVad()
