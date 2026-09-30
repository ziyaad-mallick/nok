"""CaptureLoop: the always-on knock stream.

This module had no tests at all, which is how a permanently dead microphone
stayed indistinguishable from a quiet room. Everything here fakes sounddevice
at the module seam, so it needs no microphone and no audio backend.
"""
import numpy as np
import pytest

# nok.audio imports sounddevice at module level. The tests fake it afterwards,
# but the import itself needs the package installed (it is a declared dependency,
# yet absent from a bare interpreter). Skip cleanly instead of erroring collection.
pytest.importorskip("sounddevice", reason="sounddevice not installed")

from nok import audio  # noqa: E402
from nok.ring_buffer import RingBuffer


class FakeStream:
    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeSD:
    """Stands in for the sounddevice module."""

    def __init__(self, *, fail_rates=(), default_rate=48000):
        self.fail_rates = set(fail_rates)
        self.default_rate = default_rate
        self.opened = []

    def InputStream(self, **kw):
        rate = kw["samplerate"]
        self.opened.append(rate)
        if rate in self.fail_rates:
            raise RuntimeError(f"device refused {rate}")
        return FakeStream(**kw)

    def query_devices(self, device, kind):
        return {"default_samplerate": self.default_rate}


@pytest.fixture
def ring():
    return RingBuffer(seconds=1.0)


def _loop(monkeypatch, fake, ring, **kw):
    monkeypatch.setattr(audio, "sd", fake)
    return audio.CaptureLoop(ring, device=1, **kw)


def test_opens_at_target_rate_when_device_accepts_it(monkeypatch, ring):
    fake = FakeSD()
    cap = _loop(monkeypatch, fake, ring)
    cap.start()

    assert fake.opened == [16000]
    assert cap.native_rate == 16000
    assert cap.status == audio.STATUS_RUNNING
    assert not cap.is_error


def test_falls_back_to_device_native_rate(monkeypatch, ring):
    """Built-in mic arrays commonly expose only 48kHz. Being deaf is worse."""
    fake = FakeSD(fail_rates=[16000], default_rate=48000)
    cap = _loop(monkeypatch, fake, ring)
    cap.start()

    assert fake.opened == [16000, 48000]
    assert cap.native_rate == 48000
    assert cap.status == audio.STATUS_RUNNING


def test_gives_up_and_reports_when_no_rate_works(monkeypatch, ring):
    fake = FakeSD(fail_rates=[16000, 48000], default_rate=48000)
    reasons = []
    cap = _loop(monkeypatch, fake, ring, on_error=reasons.append)
    cap.start()

    assert cap.is_error
    assert cap.status == audio.STATUS_ERROR
    # The callback is the only thing standing between a dead mic and an app
    # that looks perfectly healthy while hearing nothing, forever.
    assert len(reasons) == 1


def test_error_callback_failing_does_not_propagate(monkeypatch, ring):
    fake = FakeSD(fail_rates=[16000, 48000])

    def boom(reason):
        raise ValueError("observer is broken")

    cap = _loop(monkeypatch, fake, ring, on_error=boom)
    cap.start()  # must not raise

    assert cap.is_error


def test_user_stop_is_not_counted_as_a_failure(monkeypatch, ring):
    fake = FakeSD()
    cap = _loop(monkeypatch, fake, ring)
    cap.start()
    cap.stop()
    cap._on_finished()  # the stream's finished_callback fires on close

    assert cap.status == audio.STATUS_STOPPED
    assert not cap.is_error


def test_callback_writes_mono_downmix_at_target_rate(monkeypatch, ring):
    fake = FakeSD()
    cap = _loop(monkeypatch, fake, ring)
    cap.start()

    block = np.ones((512, 2), dtype="float32")
    block[:, 1] = 3.0  # downmix of 1 and 3 is 2
    cap._callback(block, 512, None, None)

    written = ring.read_last(512)
    assert written.shape == (512,)
    assert np.allclose(written, 2.0)


def test_callback_resamples_to_target_rate_on_the_fallback_path(monkeypatch, ring):
    """Everything downstream counts time in 16kHz samples, so the resample has
    to happen before the ring buffer, not after."""
    pytest.importorskip("soxr")
    fake = FakeSD(fail_rates=[16000], default_rate=48000)
    cap = _loop(monkeypatch, fake, ring)
    cap.start()
    assert cap.native_rate == 48000

    before = ring.total_written
    cap._callback(np.zeros((1536, 1), dtype="float32"), 1536, None, None)
    grew = ring.total_written - before

    # 1536 samples at 48k is 32ms, which is 512 samples at 16k.
    assert grew == pytest.approx(512, abs=8)
