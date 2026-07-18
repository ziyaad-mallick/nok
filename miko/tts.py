"""Text-to-speech via a short-lived subprocess (Eng Review Amendment 3).

pyttsx3 is BANNED in the long-lived worker (known SAPI repeat-call hang).
Instead we speak through a fresh `powershell` process per utterance: no
persistent SAPI state to wedge, and no extra pip dependency on the speak
path. 10s timeout + kill; subprocess.run() kills the child on timeout.

The knock detector must be gated OFF while Miko is talking so it doesn't
wake itself (OV-4). `say()` runs inside a playback gate that fires on_start
before speaking and on_end after the process exits + a 500ms cooldown.
"""
import subprocess
import time
from contextlib import contextmanager

TTS_TIMEOUT = 10
COOLDOWN_S = 0.5


def _escape_ps_single_quote(text: str) -> str:
    # In PowerShell single-quoted strings, a literal ' is written as ''.
    return text.replace("'", "''")


def _build_ps_command(text: str):
    escaped = _escape_ps_single_quote(text)
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
        f".Speak('{escaped}')"
    )
    return ["powershell", "-NoProfile", "-Command", script]


@contextmanager
def playback_gate(on_start=None, on_end=None, cooldown=COOLDOWN_S):
    """Gate the detector for a playback window + cooldown. Usable standalone
    (e.g. around chimes) or via say()."""
    if on_start:
        on_start()
    try:
        yield
    finally:
        if cooldown:
            time.sleep(cooldown)
        if on_end:
            on_end()


def say(text, on_start=None, on_end=None, cooldown=COOLDOWN_S,
        timeout=TTS_TIMEOUT, _command=None) -> str:
    """Speak `text`, gating the detector for the subprocess lifetime + cooldown.

    Returns a short result string. Never raises on TTS failure — a demo must
    not crash because the speech engine hiccuped. `_command` is a test seam
    (e.g. a deliberately hanging command to prove the timeout-kill).
    """
    cmd = _command if _command is not None else _build_ps_command(text)
    with playback_gate(on_start, on_end, cooldown):
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout)
            return "spoke"
        except subprocess.TimeoutExpired:
            # subprocess.run already killed the child on timeout.
            return "tts timed out"
        except Exception as exc:
            return f"tts failed: {exc}"
