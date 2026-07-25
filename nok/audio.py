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
        on_error=None,
    ):
        self.ring_buffer = ring_buffer
        self.device = device
        self.sample_rate = sample_rate
        # The rate we actually opened at. Differs from sample_rate only when the
        # device refused a direct open and we fell back (see _open_stream).
        self.native_rate = sample_rate
        self.blocksize = blocksize
        self.max_consecutive_failures = max_consecutive_failures
        # >1 channel is only ever requested to feed side detection; the
        # detector itself always consumes the mono downmix below.
        self.channels = max(1, int(channels))
        self.side_buffer = side_buffer
        # Called once, from the audio thread, when the stream is given up on.
        # Without it a dead mic is indistinguishable from a quiet room.
        self.on_error = on_error
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

        # The classifier is mono and tested as mono: downmix rather than
        # picking ch0, so a dead channel can't silence detection outright.
        mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
        if self.native_rate != self.sample_rate:
            # Fallback path only. Everything downstream -- the classifier's
            # timing budget, the IOI windows -- is defined in 16kHz samples, so
            # the resample has to happen before the buffer, not after.
            import soxr
            mono = soxr.resample(mono, self.native_rate, self.sample_rate)

        if self.side_buffer is not None and indata.shape[1] > 1:
            # One peak per channel, stamped with the END time of this block so
            # it lines up with the detector's own sample-derived timestamps.
            # Counted in post-resample samples so the clock stays 16kHz-based.
            self._samples_written += mono.shape[0]
            t = self._samples_written / self.sample_rate
            self.side_buffer.write(t, np.max(np.abs(indata), axis=0))

        self.ring_buffer.write(mono)

    def _on_finished(self) -> None:
        if self._stop_flag.is_set():
            return  # user-initiated stop; not a failure
        self._handle_failure("stream finished unexpectedly")

    def _start_at(self, rate: int) -> None:
        self._stream = sd.InputStream(
            samplerate=rate,
            blocksize=int(self.blocksize * rate / self.sample_rate),
            device=self.device,
            channels=self.channels,
            dtype=DTYPE,
            callback=self._callback,
            finished_callback=self._on_finished,
        )
        self._stream.start()
        self.native_rate = rate

    def _open_stream(self) -> None:
        try:
            self._start_at(self.sample_rate)
        except Exception as exc:
            # Built-in mic arrays commonly expose only 48kHz. Opening at the
            # device's own rate and resampling beats being deaf, which is what
            # this used to do -- MicStream has always had this fallback and the
            # always-on stream, the one that actually has to survive, did not.
            log_event("capture_open_failed", rate=self.sample_rate, error=str(exc))
            try:
                info = sd.query_devices(self.device, "input")
                native = int(info["default_samplerate"])
                if native == self.sample_rate:
                    raise RuntimeError("device default rate is the one that failed")
                self._start_at(native)
                log_event("capture_fallback_rate", rate=native)
            except Exception as exc2:
                self._handle_failure(f"open_stream failed: {exc2}")
                return
        self._consecutive_failures = 0
        self._status = STATUS_RUNNING

    def _handle_failure(self, reason: str) -> None:
        with self._restart_lock:
            self._consecutive_failures += 1
            attempt = self._consecutive_failures
            log_event(
                "audio_stream_failure",
                reason=reason,
                device=self.device,
                consecutive_failures=attempt,
            )
            give_up = attempt >= self.max_consecutive_failures
            if give_up:
                self._status = STATUS_ERROR
                log_event(
                    "audio_stream_error_state",
                    device=self.device,
                    consecutive_failures=attempt,
                )
            else:
                log_event("audio_stream_restart", device=self.device, attempt=attempt)

        # Everything below runs OUTSIDE the lock on purpose. Retrying re-enters
        # this method when the reopen fails, and threading.Lock is not
        # reentrant -- doing the retry inline deadlocked the process on the
        # second consecutive failure, so a mic that simply would not open hung
        # NOK at startup forever with no output at all.
        if give_up:
            if self.on_error is not None:
                # SLEEPING renders as invisible, so without this a dead stream
                # looks exactly like a healthy idle app, indefinitely.
                try:
                    self.on_error(reason)
                except Exception as exc:
                    log_event("capture_on_error_failed", error=str(exc))
            return
        self._open_stream()
