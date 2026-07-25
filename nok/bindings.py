"""Gesture -> intent. Pure mapping, no I/O, no side effects.

The spec:

    double-knock, RIGHT  -> screenshot        (local, instant)
    double-knock, LEFT   -> media play/pause  (local, instant)
    triple-knock, either -> wake the agent    (network)

Side detection is hardware-dependent (see side.py). When it is unavailable --
either the mic array is beamformed flat, or calibration never ran -- the map
degrades to a side-agnostic form rather than guessing:

    double-knock -> screenshot
    triple-knock -> wake the agent

Media control is *dropped*, not reassigned to a longer knock chain: IOI
reliability falls off past three onsets and v1 already logged spurious triples
on a hot mic. A cut feature beats one that misfires during a demo.
"""
from dataclasses import dataclass

from .ioi import DOUBLE_TAP, TRIPLE_TAP
from .side import LEFT, RIGHT, UNKNOWN

# Intents (strings so logs, tests and the overlay share one vocabulary).
SCREENSHOT = "screenshot"
MEDIA_PLAY_PAUSE = "media_play_pause"
OPEN_FAV_APP = "open_fav_app"
WAKE_AGENT = "wake_agent"
IGNORE = "ignore"

# Intents that must never touch the network, and must complete in <200ms.
INSTANT_INTENTS = frozenset({SCREENSHOT, MEDIA_PLAY_PAUSE, OPEN_FAV_APP})


@dataclass(frozen=True)
class Binding:
    intent: str
    label: str          # shown by the overlay
    instant: bool       # fires locally with no model round-trip


BINDINGS = {
    SCREENSHOT: Binding(SCREENSHOT, "Screenshot", True),
    MEDIA_PLAY_PAUSE: Binding(MEDIA_PLAY_PAUSE, "Play / Pause", True),
    OPEN_FAV_APP: Binding(OPEN_FAV_APP, "Opening", True),
    WAKE_AGENT: Binding(WAKE_AGENT, "Listening", False),
    IGNORE: Binding(IGNORE, "", True),
}


def resolve(pattern: str, side: str = UNKNOWN, *, sides_enabled: bool = True,
            fav_app: str = "") -> Binding:
    """Map a finalized knock pattern to the thing it should do.

    `sides_enabled` reflects whether side calibration actually succeeded --
    passing False forces the degraded map even if a side leaked through.

    `fav_app` is the `--fav` app name. It takes the LEFT double-knock, since
    media control is the weaker of the two things a knock can do and the user
    asked for this one by name. On hardware where sides do not work there is
    only one double-knock slot, and an explicit `--fav` wins it: a flag that
    silently does nothing on most laptops is worse than one that costs you the
    screenshot you did not ask to keep.
    """
    if pattern == TRIPLE_TAP:
        return BINDINGS[WAKE_AGENT]

    if pattern != DOUBLE_TAP:
        return BINDINGS[IGNORE]

    if not sides_enabled or side == UNKNOWN:
        return BINDINGS[OPEN_FAV_APP] if fav_app else BINDINGS[SCREENSHOT]
    if side == RIGHT:
        return BINDINGS[SCREENSHOT]
    if side == LEFT:
        return BINDINGS[OPEN_FAV_APP] if fav_app else BINDINGS[MEDIA_PLAY_PAUSE]
    return BINDINGS[SCREENSHOT]


def describe(sides_enabled: bool, agent_enabled: bool = True, fav_app: str = "",
             agent_label: str = "speak a command") -> str:
    """One-screen summary of the live gesture map, for startup output.

    Never advertises a gesture that is not actually wired up: printing
    "wake the agent" when the agent is disabled sends the user knocking at
    something that cannot answer.
    """
    left = f"open {fav_app}" if fav_app else "play / pause"
    lines = []
    if sides_enabled:
        lines.append("  knock knock  (right side)  ->  screenshot")
        lines.append(f"  knock knock  (left side)   ->  {left}")
    elif fav_app:
        lines.append(f"  knock knock                ->  open {fav_app}")
    else:
        lines.append("  knock knock                ->  screenshot")

    if agent_enabled:
        lines.append(f"  knock knock knock          ->  {agent_label}")
    else:
        lines.append("  knock knock knock          ->  (agent off)")

    notes = []
    if not sides_enabled and not fav_app:
        notes.append("side detection off -- media control unavailable")
    if not sides_enabled and fav_app:
        notes.append("side detection off -- --fav took the only double-knock, "
                     "so screenshot is unavailable")
    if not agent_enabled:
        notes.append("agent off -- no API quota; instant actions only")
    for n in notes:
        lines.append(f"  ({n})")
    return "\n".join(lines)
