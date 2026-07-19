import numpy as np

from miko.vad import (
    FRAME_MS,
    FRAME_SAMPLES,
    NO_COMMAND,
    NO_SPEECH_TIMEOUT_MS,
    TRAILING_SILENCE_MS,
    EnergyVad,
    _VadBase,
)


def test_endpoint_resets_streaming_state_at_start():
    calls = {"reset": 0}

    class _V(_VadBase):
        def reset(self):
            calls["reset"] += 1

        def is_speech(self, frame):
            return False

    out = _V().endpoint(iter([]))  # empty stream -> NO_COMMAND
    assert calls["reset"] == 1
    assert out is NO_COMMAND


def _silence():
    return np.zeros(FRAME_SAMPLES, dtype=np.float32)


def _speech():
    # loud speech-shaped frame
    t = np.arange(FRAME_SAMPLES) / 16000.0
    return (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


# -- EnergyVad classification ---------------------------------------------

def test_energy_vad_silence_is_not_speech():
    vad = EnergyVad()
    vad.is_speech(_silence())  # prime the floor on silence
    for _ in range(10):
        assert vad.is_speech(_silence()) is False


def test_energy_vad_detects_loud_frame():
    vad = EnergyVad()
    for _ in range(5):
        vad.is_speech(_silence())  # establish a low noise floor
    assert vad.is_speech(_speech()) is True


# -- endpoint state machine (driven with a scripted VAD) ------------------

class _ScriptedVad(_VadBase):
    """is_speech follows a pre-set boolean sequence, so we can test timing
    without real audio."""

    def __init__(self, flags):
        self._flags = list(flags)
        self._i = 0

    def is_speech(self, frame):
        val = self._flags[self._i] if self._i < len(self._flags) else False
        self._i += 1
        return val


def _frames(n):
    return [_silence() for _ in range(n)]


def test_endpoint_no_speech_times_out_to_no_command():
    n_frames = int(NO_SPEECH_TIMEOUT_MS / FRAME_MS) + 1
    vad = _ScriptedVad([False] * n_frames)
    assert vad.endpoint(_frames(n_frames)) is NO_COMMAND


def test_endpoint_captures_utterance_on_trailing_silence():
    # 3 speech frames, then enough silence to trip the 800ms end.
    silence_frames = int(TRAILING_SILENCE_MS / FRAME_MS) + 1
    flags = [True, True, True] + [False] * silence_frames
    vad = _ScriptedVad(flags)
    audio = vad.endpoint(_frames(len(flags)))
    assert audio is not NO_COMMAND
    # captured = 3 speech + exactly the frames needed to reach 800ms silence
    # (the endpoint breaks the moment the threshold is hit).
    cutoff_frames = int(TRAILING_SILENCE_MS / FRAME_MS)
    expected_frames = 3 + cutoff_frames
    assert audio.shape[0] == expected_frames * FRAME_SAMPLES


def test_endpoint_silence_gap_shorter_than_800ms_does_not_end():
    short_gap = int(TRAILING_SILENCE_MS / FRAME_MS) - 2  # under threshold
    flags = [True] + [False] * short_gap + [True, True] + \
        [False] * (int(TRAILING_SILENCE_MS / FRAME_MS) + 1)
    vad = _ScriptedVad(flags)
    audio = vad.endpoint(_frames(len(flags)))
    assert audio is not NO_COMMAND
    # speech resumed after the short gap, so the whole thing is one utterance
    assert audio.shape[0] > (1 + short_gap) * FRAME_SAMPLES
