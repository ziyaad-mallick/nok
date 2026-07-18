"""Environment sanity check: import every dependency, load models, open audio.

Prints one OK/FAIL/SKIP line per check plus versions. Exits nonzero if any
check FAILs (SKIP does not count as failure — used when silero-vad's pip
package could not be installed without pulling in torch, per project
policy of staying torch-free; energy endpointing is the fallback in that
case).
"""
import sys
import time

results = []  # (name, "OK"|"FAIL"|"SKIP", detail)


def check(name):
    def decorator(fn):
        try:
            detail = fn()
            results.append((name, "OK", detail or ""))
        except Exception as exc:
            results.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))
        return fn
    return decorator


@check("numpy")
def _():
    import numpy
    return f"v{numpy.__version__}"


@check("sounddevice")
def _():
    import sounddevice
    return f"v{sounddevice.__version__}"


@check("librosa")
def _():
    import librosa
    return f"v{librosa.__version__}"


@check("onnxruntime")
def _():
    import onnxruntime
    return f"v{onnxruntime.__version__}"


@check("faster_whisper (import)")
def _():
    import faster_whisper
    return f"v{faster_whisper.__version__}"


@check("mss")
def _():
    import mss
    return f"v{mss.__version__}"


@check("pynput")
def _():
    import pynput
    from importlib.metadata import version
    return f"v{version('pynput')}"


@check("pyttsx3")
def _():
    import pyttsx3
    from importlib.metadata import version
    return f"v{version('pyttsx3')}"


@check("pytest")
def _():
    import pytest
    return f"v{pytest.__version__}"


def _silero_vad_onnx_check():
    """silero-vad's pip package hard-requires torch/torchaudio in its base
    install (Requires-Dist: torch>=1.12.0, torchaudio>=0.12.0 — confirmed
    against silero-vad 6.2.1 metadata). This project stays torch-free, so
    silero-vad is not installed and this check is a SKIP, not a FAIL.
    Energy endpointing is the fallback voice-activity detector.
    """
    try:
        import importlib
        importlib.import_module("silero_vad")
    except ImportError:
        results.append((
            "silero-vad (onnx)",
            "SKIP",
            "not installed: pip package requires torch/torchaudio as a hard "
            "dependency; staying torch-free per project policy. Fallback: "
            "energy endpointing.",
        ))
        return
    from silero_vad import load_silero_vad
    load_silero_vad(onnx=True)
    results.append(("silero-vad (onnx)", "OK", "loaded via onnx"))


@check("faster_whisper small.en (load)")
def _():
    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel("small.en", device="cpu", compute_type="int8")
    elapsed = time.time() - t0
    del model
    return f"loaded in {elapsed:.2f}s (device=cpu, compute_type=int8)"


@check("sounddevice InputStream (1s, default device)")
def _():
    import sounddevice as sd
    with sd.InputStream(samplerate=16000, channels=1, dtype="float32"):
        time.sleep(1.0)
    return "opened + closed OK"


def main():
    _silero_vad_onnx_check()

    name_w = max(len(n) for n, _, _ in results)
    any_fail = False
    for name, status, detail in results:
        print(f"[{status:4}] {name.ljust(name_w)}  {detail}")
        if status == "FAIL":
            any_fail = True

    print()
    if any_fail:
        print("RESULT: FAIL (one or more checks failed)")
        sys.exit(1)
    else:
        print("RESULT: OK (all checks passed or were explicitly skipped)")
        sys.exit(0)


if __name__ == "__main__":
    main()
