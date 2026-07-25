"""Replay a scripted conversation through the real overlay window.

Opens a window and BLOCKS — not for headless/automated contexts. Needs no API
key, no microphone and no knocking, so it is the fastest way to QA the visual
language or record b-roll.

    .venv/Scripts/python.exe scripts/overlay_preview.py

A background thread drives states AND synthesises speech-shaped amplitude, so
the audio-reactive rings and orb behave exactly as they do against a live
session. The syllable envelope is what makes this a real preview rather than a
slideshow — a flat level would make every state look alive and hide exactly the
bugs this exists to catch.
"""
import math
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nok.overlay import Overlay  # noqa: E402

# (state, detail, seconds, audio_source or None)
SCRIPT = [
    ("SLEEPING",  "ready",                      3.0, None),
    ("WOKE",      "summoned",                   0.3, None),
    ("LISTENING", "woke on TRIPLE_TAP",         3.5, "mic"),
    ("THINKING",  "what's on my screen?",       1.2, None),
    ("SPEAKING",  "",                           5.0, "out"),
    ("LISTENING", "your turn",                  2.5, "mic"),
    ("SPEAKING",  "",                           3.5, "out"),
    ("ACTING",    "screenshot",                 0.8, None),
    ("SLEEPING",  "session closed",             3.0, None),
    ("WOKE",      "summoned",                   0.3, None),
    ("LISTENING", "woke on DOUBLE_TAP",         1.5, "mic"),
    ("ERROR",     "socket closed unexpectedly", 4.5, None),
    ("SLEEPING",  "recovered",                  3.0, None),
]

TICK = 1 / 30.0


def speech_envelope(t):
    """Amplitude shaped like talking: ~4Hz syllables inside phrases, with real
    gaps for breath between them."""
    phrase = 0.0 if (t % 4.2) > 3.2 else 1.0
    syllable = 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t)
    jitter = random.uniform(0.85, 1.15)
    return phrase * (0.05 + 0.28 * syllable ** 2) * jitter


def drive(overlay, stop):
    time.sleep(0.6)  # let the window map before the first beat
    while not stop.is_set():
        for state, detail, hold, source in SCRIPT:
            if stop.is_set():
                return
            overlay.set_state(state, detail)
            t0 = time.monotonic()
            while time.monotonic() - t0 < hold:
                if stop.is_set():
                    return
                if source:
                    overlay.set_level(source, speech_envelope(time.monotonic() - t0))
                time.sleep(TICK)


BANNER = """
==============================================================
  OVERLAY PREVIEW - this is a SCRIPTED ANIMATION, not the app.
==============================================================
  It does NOT listen to your microphone.  Knocking does nothing.
  It does NOT take screenshots.  The "screenshot" beat is fake.
  It does NOT play audio.  The SPEAKING state is silent by design.

  It exists only to check colour, motion, position and timing.

  To run NOK for real:      .venv\\Scripts\\python.exe -m nok
==============================================================
"""


def main():
    print(BANNER)
    overlay = Overlay()
    stop = threading.Event()
    threading.Thread(target=drive, args=(overlay, stop), daemon=True).start()
    print("Preview running. Ctrl-C to quit.")
    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        overlay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
