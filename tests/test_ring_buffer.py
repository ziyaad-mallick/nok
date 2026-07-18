import threading

import numpy as np
import pytest

from miko.ring_buffer import RingBuffer


def test_wraparound_correctness():
    rb = RingBuffer(seconds=1.0, sample_rate=10)  # capacity 10
    rb.write(np.arange(0, 8, dtype=np.float32))  # fills 0..7
    rb.write(np.arange(8, 14, dtype=np.float32))  # wraps: writes 8,9,10,11,12,13
    # capacity 10, total written 14, buffer should hold the last 10 samples: 4..13
    last10 = rb.read_last(1.0)  # 1s * 10Hz = 10 samples = full capacity
    assert last10.shape[0] == 10
    np.testing.assert_array_equal(last10, np.arange(4, 14, dtype=np.float32))
    assert rb.total_written == 14


def test_read_last_window_contents():
    rb = RingBuffer(seconds=2.0, sample_rate=10)  # capacity 20
    data = np.arange(0, 15, dtype=np.float32)
    rb.write(data)
    last_half_second = rb.read_last(0.5)  # 5 samples
    np.testing.assert_array_equal(last_half_second, data[-5:])

    full = rb.read_last(2.0)  # more than written (15 < 20)
    np.testing.assert_array_equal(full, data)


def test_read_last_more_than_written_returns_available_only():
    rb = RingBuffer(seconds=1.0, sample_rate=10)
    rb.write(np.array([1.0, 2.0, 3.0], dtype=np.float32))
    out = rb.read_last(1.0)
    assert out.shape[0] == 3
    np.testing.assert_array_equal(out, [1.0, 2.0, 3.0])


def test_block_larger_than_capacity():
    rb = RingBuffer(seconds=1.0, sample_rate=10)  # capacity 10
    data = np.arange(0, 25, dtype=np.float32)
    rb.write(data)
    out = rb.read_last(1.0)
    np.testing.assert_array_equal(out, data[-10:])
    assert rb.total_written == 25


def test_concurrent_writer_reader_hammering():
    rb = RingBuffer(seconds=1.0, sample_rate=16000)
    n_blocks = 2000
    block_size = 512
    errors = []

    def writer():
        try:
            for _ in range(n_blocks):
                block = np.random.rand(block_size).astype(np.float32)
                rb.write(block)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader():
        try:
            for _ in range(n_blocks):
                out = rb.read_last(0.1)
                assert out.dtype == np.float32
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(2)] + [
        threading.Thread(target=reader) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert rb.total_written == n_blocks * block_size * 2
