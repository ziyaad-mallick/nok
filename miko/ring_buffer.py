"""Thread-safe fixed-size ring buffer for float32 mono audio at a fixed sample rate."""
import threading

import numpy as np

SAMPLE_RATE = 16000


class RingBuffer:
    """Fixed-capacity circular buffer of float32 audio samples.

    Sized for `seconds` seconds at `sample_rate` Hz. Safe to call `write` from
    one thread (e.g. an audio callback) and `read_last` / `total_written` from
    another.
    """

    def __init__(self, seconds: float, sample_rate: int = SAMPLE_RATE):
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.sample_rate = sample_rate
        self.capacity = int(round(seconds * sample_rate))
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._write_pos = 0
        self._total_written = 0
        self._lock = threading.Lock()

    def write(self, block: np.ndarray) -> None:
        """Append a 1-D float32 block into the buffer, wrapping around as needed."""
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        n = block.shape[0]
        if n == 0:
            return
        with self._lock:
            if n >= self.capacity:
                # Only the tail fits; buffer becomes exactly this tail.
                self._buf[:] = block[-self.capacity:]
                self._write_pos = 0
            else:
                pos = self._write_pos
                end = pos + n
                if end <= self.capacity:
                    self._buf[pos:end] = block
                else:
                    first_len = self.capacity - pos
                    self._buf[pos:self.capacity] = block[:first_len]
                    self._buf[0:end - self.capacity] = block[first_len:]
                self._write_pos = end % self.capacity
            self._total_written += n

    def read_last(self, seconds: float) -> np.ndarray:
        """Return a contiguous copy of the last `seconds` seconds of audio.

        If fewer samples than requested have ever been written, returns all
        samples written so far (zero-padded at the front is NOT performed;
        the returned array is simply shorter).
        """
        if seconds <= 0:
            return np.zeros(0, dtype=np.float32)
        n_requested = int(round(seconds * self.sample_rate))
        with self._lock:
            n_available = min(n_requested, self.capacity, self._total_written)
            if n_available <= 0:
                return np.zeros(0, dtype=np.float32)
            pos = self._write_pos
            start = (pos - n_available) % self.capacity
            if start + n_available <= self.capacity:
                out = self._buf[start:start + n_available].copy()
            else:
                first_len = self.capacity - start
                out = np.empty(n_available, dtype=np.float32)
                out[:first_len] = self._buf[start:self.capacity]
                out[first_len:] = self._buf[0:n_available - first_len]
            return out

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._total_written
