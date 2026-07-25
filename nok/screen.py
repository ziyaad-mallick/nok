"""Screen capture — the agent's eyes, and the instant screenshot action.

Two consumers with opposite needs:

  * the model wants a SMALL image, fast. Raw screenshots are multi-megabyte;
    at that size upload dominates response latency. Downscale to ~1280px and
    JPEG it.
  * the user wants their screenshot at full resolution, unmangled.

Both are latency-critical, so the expensive setup is paid once at startup
rather than on the first knock. Measured on the dev machine:

    cold (import + first MSS instance)  ~900 ms   <- what the user used to feel
    warm grab                            ~40 ms
    resize + JPEG encode                 ~30 ms

`mss` instances are not thread-safe, so each thread gets its own via
threading.local — the agent path and the instant-action path run on different
threads.
"""
import base64
import io
import threading
from datetime import datetime
from pathlib import Path

MAX_EDGE = 1280      # long-edge target before JPEG encode
JPEG_QUALITY = 80    # readable UI text, ~10x smaller than PNG

# optimize=True costs ~45ms to save ~4KB. Not a trade worth making on a path
# the user is waiting on.
JPEG_OPTIMIZE = False

_local = threading.local()


def _sct():
    """This thread's MSS instance, created once."""
    inst = getattr(_local, "sct", None)
    if inst is None:
        import mss
        # mss.mss is deprecated in favour of mss.MSS.
        factory = getattr(mss, "MSS", None) or mss.mss
        inst = factory()
        _local.sct = inst
    return inst


def _primary(sct):
    """monitors[0] is the virtual bounding box of ALL displays, not a screen.
    monitors[1] is the primary display; fall back if only one entry exists."""
    return sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]


def warmup():
    """Pay the import + instance cost at startup, not on the first knock."""
    try:
        from PIL import Image  # noqa: F401
        sct = _sct()
        sct.grab(_primary(sct))
        return True
    except Exception:
        return False


def grab_jpeg_b64(max_edge: int = MAX_EDGE, quality: int = JPEG_QUALITY) -> str:
    """Capture, downscale, JPEG-encode, base64."""
    from PIL import Image

    sct = _sct()
    shot = sct.grab(_primary(sct))
    img = Image.frombytes("RGB", shot.size, shot.rgb)

    long_edge = max(img.size)
    if long_edge > max_edge:
        scale = max_edge / long_edge
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,  # worth its ~25ms: the model has to read UI text
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=JPEG_OPTIMIZE)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def data_url(max_edge: int = MAX_EDGE, quality: int = JPEG_QUALITY) -> str:
    """The exact string the Realtime API's input_image content part wants."""
    return f"data:image/jpeg;base64,{grab_jpeg_b64(max_edge, quality)}"


def save_png(folder: Path = None) -> Path:
    """Full-resolution screenshot to disk — the double-knock instant action.

    Kept separate from the agent path: this one is the user's to keep, so it is
    never downscaled or recompressed.
    """
    import mss.tools

    folder = folder or (Path.home() / "Pictures" / "nok")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"nok-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    sct = _sct()
    shot = sct.grab(_primary(sct))
    mss.tools.to_png(shot.rgb, shot.size, output=str(dest))
    return dest
