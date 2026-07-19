"""Resolve the Codex CLI to a runnable argv prefix.

On Windows, npm installs `codex` as a .cmd shim that subprocess.run cannot
launch without a shell (WinError 2). Prefer the real vendored codex.exe so
JSON prompt args pass through without cmd.exe re-quoting. Honors a CODEX_BIN
env override. The result is cached for the process lifetime.
"""
import os
import shutil
from pathlib import Path
from typing import Optional

_CACHED: Optional[list] = None


def resolve_codex() -> list:
    """Return the argv prefix that launches codex (e.g. ["C:\\...\\codex.exe"]).

    Raises FileNotFoundError if codex cannot be located.
    """
    global _CACHED
    if _CACHED is not None:
        return _CACHED

    override = os.environ.get("CODEX_BIN")
    if override and Path(override).exists():
        _CACHED = [override]
        return _CACHED

    exe = shutil.which("codex.exe")
    if exe:
        _CACHED = [exe]
        return _CACHED

    generic = shutil.which("codex")
    if generic:
        if os.name == "nt" and generic.lower().endswith((".cmd", ".bat")):
            for cand in Path(generic).parent.glob(
                "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe"
            ):
                _CACHED = [str(cand)]
                return _CACHED
            _CACHED = ["cmd", "/c", generic]
            return _CACHED
        _CACHED = [generic]
        return _CACHED

    raise FileNotFoundError(
        "codex CLI not found on PATH. Install with `npm i -g @openai/codex`, run "
        "`codex login`, or set CODEX_BIN to the codex executable path."
    )
