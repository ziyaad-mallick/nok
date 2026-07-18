"""One keyword matcher (Eng Review Amendment 5, DRY).

Serves BOTH the quota-exhausted fallback (transcript -> action without the
model) and skill KEYWORDS routing (the manifest fed to the prompt). A single
registry of {name: keywords}; each name carries the action dict to run when
it wins. match() returns the best action dict (a copy) or None.
"""
import copy
import re


def _words(text: str):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


class Matcher:
    def __init__(self):
        self._registry = {}  # name -> {"keywords": [...], "action": {...}}

    def register(self, name: str, keywords, action: dict) -> None:
        self._registry[name] = {
            "keywords": [k.lower() for k in keywords],
            "action": action,
        }

    def match(self, transcript: str):
        """Return the highest-scoring registered action dict, or None.

        Score = number of registered keywords present as whole words in the
        transcript. Ties break on registration order; zero score -> None.
        """
        tokens = set(_words(transcript))
        if not tokens:
            return None
        best_name, best_score = None, 0
        for name, entry in self._registry.items():
            score = sum(1 for kw in entry["keywords"] if kw in tokens)
            if score > best_score:
                best_name, best_score = name, score
        if best_name is None:
            return None
        return copy.deepcopy(self._registry[best_name]["action"])


def build_default_matcher(apps: dict) -> Matcher:
    """Build the fallback matcher from the live toolbox verbs + app names."""
    m = Matcher()
    m.register("mute", ["mute", "silence", "quiet"],
               {"action": "media", "args": {"command": "mute"}, "say": "Muting."})
    m.register("play", ["play", "resume", "unpause"],
               {"action": "media", "args": {"command": "play"}, "say": "Playing."})
    m.register("pause", ["pause", "stop"],
               {"action": "media", "args": {"command": "pause"}, "say": "Pausing."})
    m.register("next", ["next", "skip"],
               {"action": "media", "args": {"command": "next"}, "say": "Next track."})
    for name in apps:
        m.register(
            f"open_{name}",
            ["open", "launch", "start", name],
            {"action": "open_app", "args": {"name": name}, "say": f"Opening {name}."},
        )
    return m
