"""Persistent microphone capture loop feeding a RingBuffer.

The audio callback is kept allocation-free: no prints, no logging calls,
just a ring-buffer write and (on status flags) a plain attribute set. All
restart/error logging happens outside the callback, from _on_finished.
"""
import threading

import numpy as np
import sounddevice as sd

from .logs import log_event
from .ring_buffer import SAMPLE_RATE, RingBuffer

BLOCKSIZE = 512
CHANNELS = 1
DTYPE = "float32"
MAX_CHANNELS = 4  # Intel Smart Sound arrays expose 4; we only compare two
MAX_CONSECUTIVE_FAILURES = 3

STATUS_STOPPED = "STOPPED"
STATUS_RUNNING = "RUNNING"
STATUS_ERROR = "ERROR"


class CaptureLoop:
    """Owns one sounddevice.InputStream pinned to an explicit device index.

    If the stream dies (device change, sleep/wake, driver hiccup) it is
    auto-restarted on the same device, up to `max_consecutive_failures` in a
    row. After that many straight failures, `status` becomes STATUS_ERROR
    and no further restarts are attempted until `start()` is called again.
    """

    def __init__(
        self,
        ring_buffer: RingBuffer,
        device: int,
        sample_rate: int = SAMPLE_RATE,
        blocksize: int = BLOCKSIZE,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        channels: int = CHANNELS,
        side_buffer=None,
    ):
        self.ring_buffer = ring_buffer
        self.device = device
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.max_consecutive_failures = max_consecutive_failures
        # >1 channel is only ever requested to feed side detection; the
        # detector itself always consumes the mono downmix below.
        self.channels = max(1, int(channels))
        self.side_buffer = side_buffer
        self._samples_written = 0

        self._stream = None
        self._consecutive_failures = 0
        self._restart_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._status = STATUS_STOPPED
        self.last_status_flags = None  # set (not logged) inside the callback

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_error(self) -> bool:
        return self._status == STATUS_ERROR

    def start(self) -> None:
        self._stop_flag.clear()
        self._consecutive_failures = 0
        self._open_stream()

    def stop(self) -> None:
        self._stop_flag.set()
        self._status = STATUS_STOPPED
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # -- internal --------------------------------------------------------

    def _callback(self, indata, frames, time_info, status):
        # NOTE: kept cheap by design. No prints, no logging calls here.
        if status:
            self.last_status_flags = status

        if self.side_buffer is not None and indata.shape[1] > 1:
            # One peak per channel, stamped with the END time of this block so
            # it lines up with the detector's own sample-derived timestamps.
            self._samples_written += frames
            t = self._samples_written / self.sample_rate
            self.side_buffer.write(t, np.max(np.abs(indata), axis=0))

        # The classifier is mono and tested as mono: downmix rather than
        # picking ch0, so a dead channel can't silence detection outright.
        mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
        self.ring_buffer.write(mono)

    def _on_finished(self) -> None:
        if self._stop_flag.is_set():
            return  # user-initiated stop; not a failure
        self._handle_failure("stream finished unexpectedly")

    def _open_stream(self) -> None:
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                device=self.device,
                channels=self.channels,
                dtype=DTYPE,
                callback=self._callback,
                finished_callback=self._on_finished,
            )
            self._stream.start()
            self._consecutive_failures = 0
            self._status = STATUS_RUNNING
        except Exception as exc:
            self._handle_failure(f"open_stream failed: {exc}")

    def _handle_failure(self, reason: str) -> None:
        with self._restart_lock:
            self._consecutive_failures += 1
            log_event(
                "audio_stream_failure",
                reason=reason,
                device=self.device,
                consecutive_failures=self._consecutive_failures,
            )
            if self._consecutive_failures >= self.max_consecutive_failures:
                self._status = STATUS_ERROR
                log_event(
                    "audio_stream_error_state",
                    device=self.device,
                    consecutive_failures=self._consecutive_failures,
                )
                return
            log_event(
                "audio_stream_restart",
                device=self.device,
                attempt=self._consecutive_failures,
            )
            self._open_stream()
