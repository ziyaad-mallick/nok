"""Screen capture for the agent's eyes.

One job: turn the primary monitor into a base64 JPEG small enough to reach the
model quickly. Raw screenshots are multi-megabyte; at that size the upload
dominates the response latency and can trip WebSocket payload limits. Downscaling
to ~1280px on the long edge is the single biggest lever on "how fast does the
agent see my screen".
"""
import base64
import io
from datetime import datetime
from pathlib import Path

MAX_EDGE = 1280      # long-edge target before JPEG encode
JPEG_QUALITY = 80    # visually lossless enough to read UI text, ~10x smaller


def _primary_monitor(sct):
    """monitors[0] is the virtual bounding box of ALL displays, not a screen.
    monitors[1] is the primary display; fall back if only one entry exists."""
    return sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]


def grab_raw():
    """Capture the primary monitor. Returns (rgb_bytes, (w, h))."""
    import mss

    with mss.mss() as sct:
        shot = sct.grab(_primary_monitor(sct))
        return shot.rgb, shot.size


def grab_jpeg_b64(max_edge: int = MAX_EDGE, quality: int = JPEG_QUALITY) -> str:
    """Capture, downscale, JPEG-encode, base64. Ready for a data: URL."""
    from PIL import Image

    rgb, size = grab_raw()
    img = Image.frombytes("RGB", size, rgb)

    long_edge = max(img.size)
    if long_edge > max_edge:
        scale = max_edge / long_edge
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def data_url(max_edge: int = MAX_EDGE, quality: int = JPEG_QUALITY) -> str:
    """The exact string the Realtime API's input_image content part wants."""
    return f"data:image/jpeg;base64,{grab_jpeg_b64(max_edge, quality)}"


def save_png(folder: Path = None) -> Path:
    """Full-resolution screenshot to disk — the double-knock instant action.

    Deliberately separate from the agent path: this one is for the user to
    keep, so it is not downscaled or recompressed.
    """
    import mss
    import mss.tools

    folder = folder or (Path.home() / "Pictures" / "miko")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"miko-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    with mss.mss() as sct:
        shot = sct.grab(_primary_monitor(sct))
        mss.tools.to_png(shot.rgb, shot.size, output=str(dest))
    return dest
