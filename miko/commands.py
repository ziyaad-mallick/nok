"""Spoken phrase -> intent. Pure functions, no I/O.

Deliberately not an LLM. A local model costs ~4s per turn and can hallucinate an
action that was never asked for; matching costs microseconds and can only ever
return something in this file. For a fixed command surface that is the correct
trade — the model would be adding latency and risk to buy flexibility nobody
asked for.

Whisper output is messy in specific, predictable ways, and each of these is
handled below rather than hoped away:
  * trailing punctuation            "screenshot."
  * filler and politeness           "uh, can you take a screenshot please"
  * homophones on short commands    "mute" -> "moot", "next" -> "necks"
  * number words                    "volume up two" -> repeat twice

Anything unmatched returns UNKNOWN. Miko says so rather than guessing, and
never types an unrecognised sentence into whatever window happens to be
focused — that is how a stray phrase ends up executed in a terminal.
"""
import re

SCREENSHOT = "screenshot"
MEDIA = "media"
OPEN_APP = "open_app"
TYPE_TEXT = "type_text"
UNKNOWN = "unknown"

# Filler stripped before matching. Ordered longest-first so "can you please"
# does not leave a dangling "please".
_FILLER = [
    "could you please", "can you please", "would you please",
    "could you", "can you", "would you", "please", "hey miko", "miko",
    "i want you to", "i need you to", "go ahead and", "just",
    "um", "uh", "erm", "like",
]

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1, "once": 1, "twice": 2,
}

# Whisper mishears short commands consistently. Map the observed variants.
_HOMOPHONES = {
    "moot": "mute", "mut": "mute", "muted": "mute",
    "necks": "next", "nest": "next",
    "prev": "previous", "pre": "previous",
    "paws": "pause", "pours": "pause",
    "shot": "screenshot", "screen shot": "screenshot", "screencap": "screenshot",
    "volume of": "volume up",
}

_MEDIA_PATTERNS = [
    (r"\b(play|pause|resume|stop|hold on)\b", "play"),
    (r"\b(next|skip|forward)\b.*\b(track|song|one)?\b", "next"),
    (r"\b(previous|back|last)\b.*\b(track|song|one)?\b", "previous"),
    (r"\b(mute|silence|shut up|quiet)\b", "mute"),
    (r"\b(unmute)\b", "mute"),
    (r"\bvolume\s+(up|louder|higher)\b", "volume_up"),
    (r"\b(louder|turn it up)\b", "volume_up"),
    (r"\bvolume\s+(down|lower|quieter)\b", "volume_down"),
    (r"\b(quieter|turn it down)\b", "volume_down"),
]

_SCREENSHOT_PATTERN = re.compile(
    r"\b(screenshot|screen ?grab|capture (the )?screen|grab (the )?screen"
    r"|take a (picture|shot) of (the )?screen)\b"
)

_OPEN_PATTERN = re.compile(r"\b(?:open|launch|start|run|fire up)\s+(?P<name>.+)$")

_TYPE_PATTERN = re.compile(
    r"\b(?:type|write|dictate|insert|say)\s+(?:out\s+)?(?:this[:,]?\s+)?(?P<text>.+)$"
)


def normalize(text):
    """Lowercase, strip punctuation and filler, fix known mishearings."""
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\s']", " ", t)          # punctuation -> space
    t = re.sub(r"\s+", " ", t).strip()

    for wrong, right in _HOMOPHONES.items():
        t = re.sub(rf"\b{re.escape(wrong)}\b", right, t)

    for f in _FILLER:
        t = re.sub(rf"\b{re.escape(f)}\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _repeat_count(text):
    """'volume up three' / 'next twice' -> how many times to fire the key."""
    m = re.search(r"\b(\d+)\s*(?:times?)?\s*$", text)
    if m:
        return max(1, min(10, int(m.group(1))))
    for word, n in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\s*(?:times?)?\s*$", text):
            return max(1, min(10, n))
    return 1


def parse(transcript):
    """Return (intent, args). Never raises, never guesses."""
    raw = (transcript or "").strip()
    text = normalize(raw)
    if not text:
        return UNKNOWN, {}

    # Dictation first: "type screenshot" must type the word, not fire the
    # action. The explicit verb is a stronger signal than any keyword inside it.
    m = _TYPE_PATTERN.search(text)
    if m:
        # Re-extract from the ORIGINAL text so casing and punctuation survive.
        raw_m = _TYPE_PATTERN.search(re.sub(r"\s+", " ", raw.strip()), )
        body = (raw_m.group("text") if raw_m else m.group("text")).strip()
        body = body.rstrip(".") if body.endswith("..") else body
        return (TYPE_TEXT, {"text": body}) if body else (UNKNOWN, {})

    if _SCREENSHOT_PATTERN.search(text):
        return SCREENSHOT, {}

    for pattern, command in _MEDIA_PATTERNS:
        if re.search(pattern, text):
            return MEDIA, {"command": command, "repeat": _repeat_count(text)}

    m = _OPEN_PATTERN.search(text)
    if m:
        name = m.group("name").strip()
        name = re.sub(r"\b(app|application|program|for me|up)\b", " ", name)
        name = re.sub(r"^(the|my|a|an)\s+", "", name.strip())
        name = re.sub(r"\s+", " ", name).strip()
        return (OPEN_APP, {"name": name}) if name else (UNKNOWN, {})

    return UNKNOWN, {}


def describe(intent, args):
    """Short confirmation line, spoken and shown on the overlay."""
    if intent == SCREENSHOT:
        return "Screenshot saved."
    if intent == MEDIA:
        n = args.get("repeat", 1)
        cmd = args.get("command", "").replace("_", " ")
        return f"{cmd.capitalize()}." if n == 1 else f"{cmd.capitalize()} {n} times."
    if intent == OPEN_APP:
        return f"Opening {args.get('name')}."
    if intent == TYPE_TEXT:
        text = args.get("text", "")
        return f"Typed {len(text)} characters."
    return "I didn't catch a command I know."
