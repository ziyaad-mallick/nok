"""Replay a scripted demo through the real overlay window so the user can
screen-record / QA it. Opens a window and BLOCKS — do not run this in a headless
or automated context.

    .venv/Scripts/python.exe scripts/overlay_preview.py

A background thread drives the states on a timeline (the overlay's set_state is
thread-safe); the main thread owns the tkinter mainloop. The THINKING beat is a
deliberate ~25s hold so you can verify the animation stays alive/honest through
a realistic model latency. Ctrl-C to quit early.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miko.overlay import Overlay  # noqa: E402

# (delay_before, state, detail) — delay is seconds to wait before sending it.
SCRIPT = [
    (0.0, "SLEEPING", "ready — knock twice"),
    (6.0, "LISTENING", "woke on DOUBLE_TAP"),
    (3.0, "THINKING", "open spotify"),
    (25.0, "ACTING", "open_app"),          # ~25s honest THINKING hold
    (3.0, "SLEEPING", "cycle complete"),   # -> DONE resolve, then idle
    (5.0, "LISTENING", "woke on TRIPLE_TAP"),
    (2.5, "TEACHING", "learn a new trick: greet the room"),
    (8.0, "SLEEPING", "cycle complete"),   # -> DONE resolve, then idle
    (5.0, "ERROR", "brain timed out"),
    (3.0, "SLEEPING", "cycle complete"),   # -> sticky error, then idle
    (8.0, "SLEEPING", "cycle complete"),   # settle
]


def _drive(overlay, stop):
    for delay, state, detail in SCRIPT:
        if stop.wait(delay):
            return
        overlay.set_state(state, detail)


def main():
    overlay = Overlay()
    stop = threading.Event()
    driver = threading.Thread(target=_drive, args=(overlay, stop), daemon=True)
    driver.start()
    print("Overlay preview running. Screen-record now. Ctrl-C to quit.")
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
