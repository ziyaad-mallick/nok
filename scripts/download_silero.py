"""Download the pinned silero-vad ONNX model into models/ (gitignored).

URL verified 2026-07-19 against the snakers4/silero-vad repo, release tag
v6.2.1 (HTTP 200, 2327524 bytes). Run once before the demo:

    python scripts/download_silero.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miko.vad import DEFAULT_MODEL_PATH, download_silero  # noqa: E402


def main():
    dest = download_silero(DEFAULT_MODEL_PATH)
    print(f"OK: silero model at {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
