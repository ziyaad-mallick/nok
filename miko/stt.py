"""faster-whisper transcription wrapper.

The model is loaded ONCE at init from the pre-downloaded HF cache (never a
live download at runtime — Deep Review error row). transcribe() returns the
text plus a small confidence-ish info dict.

`python -m miko.stt --bench` times small.en vs base.en on a synthetic clip so
the size choice is measured on THIS machine (Amendment 9), not assumed.
"""
import argparse
import sys
import time
import types

import numpy as np

from .logs import log_event
from .ring_buffer import SAMPLE_RATE


def _stub_av_if_blocked():
    """faster-whisper force-imports PyAV (`av`) at package load, but only uses
    it to decode audio *files* -- Miko always hands the model a numpy array, so
    PyAV is never actually called. On this machine Windows Smart App Control
    blocks PyAV's unsigned native DLL ("An Application Control policy has blocked
    this file"), which would otherwise make Whisper unimportable and take the
    whole app down. If the real `av` can't load, install a stub so the import
    succeeds; decode_audio is never reached on our numpy path.
    """
    if "av" in sys.modules:
        return
    try:
        import av  # noqa: F401
    except Exception as exc:  # ImportError from the SAC DLL block, etc.
        sys.modules["av"] = types.ModuleType("av")
        log_event("av_stubbed", reason=str(exc))


class Transcriber:
    def __init__(self, model_size="small.en", compute_type="int8", device="cpu"):
        _stub_av_if_blocked()
        from faster_whisper import WhisperModel

        self.model_size = model_size
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, np_audio_16k):
        """Return (text, info). `info` carries avg_logprob, a 0-1 confidence
        estimate (exp of mean segment logprob), and no_speech_prob.
        """
        audio = np.asarray(np_audio_16k, dtype=np.float32).reshape(-1)
        segments, _ = self._model.transcribe(audio, language="en")
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()

        if segs:
            avg_logprob = float(np.mean([s.avg_logprob for s in segs]))
            no_speech = float(np.mean([s.no_speech_prob for s in segs]))
            confidence = float(np.exp(avg_logprob))
        else:
            avg_logprob, no_speech, confidence = 0.0, 1.0, 0.0

        return text, {
            "avg_logprob": avg_logprob,
            "no_speech_prob": no_speech,
            "confidence": confidence,
            "n_segments": len(segs),
        }


def _synthetic_clip(seconds=3.0):
    """A 3s speech-shaped clip: amplitude-modulated tone + noise. Good enough
    to time the models on-machine (we measure latency, not accuracy)."""
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    tone = 0.3 * np.sin(2 * np.pi * 180 * t)
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)  # ~3 Hz syllable rate
    noise = 0.02 * np.random.randn(n)
    return (tone * envelope + noise).astype(np.float32)


def _bench():
    clip = _synthetic_clip()
    print(f"benchmarking on a {clip.shape[0] / SAMPLE_RATE:.1f}s synthetic clip\n")
    print(f"{'model':>10}  {'load_s':>8}  {'xcribe_s':>9}")
    for size in ("small.en", "base.en"):
        t0 = time.time()
        tr = Transcriber(model_size=size)
        load_s = time.time() - t0
        t1 = time.time()
        tr.transcribe(clip)
        xcribe_s = time.time() - t1
        print(f"{size:>10}  {load_s:>8.2f}  {xcribe_s:>9.2f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", action="store_true",
                        help="time small.en vs base.en on a synthetic clip")
    args = parser.parse_args()
    if args.bench:
        _bench()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
