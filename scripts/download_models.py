"""Pre-download faster-whisper models so nothing downloads at runtime.

Downloads "small.en" and "base.en" (compute_type int8, device cpu) into the
default Hugging Face cache. Run once before demo day.
"""
from faster_whisper import WhisperModel

MODELS = ["small.en", "base.en"]


def main():
    for name in MODELS:
        print(f"Downloading {name} ...")
        model = WhisperModel(name, device="cpu", compute_type="int8")
        print(f"  OK: {name} loaded (compute_type=int8, device=cpu)")
        del model
    print("Done. Models cached in the default Hugging Face cache "
          "(usually %USERPROFILE%\\.cache\\huggingface).")


if __name__ == "__main__":
    main()
