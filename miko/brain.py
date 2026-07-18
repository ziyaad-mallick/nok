"""The model bridge.

Brain.decide(transcript, context) -> a VALIDATED action dict.

Two implementations:
  - MockBrain: pattern->response script from tests/fixtures/mock_brain.json.
    The dev/test brain (zero spend). Supports misbehavior modes so the rest
    of the pipeline can be exercised against a hostile model: prose-wrapped
    JSON, no JSON at all, timeout, and quota-error text.
  - CodexBrain: shells out to `codex exec ... -o <tmpfile>` and parses the
    -o file (Amendment 2). Split timeouts (Amendment 10): TIMEOUT_INTENT=60s
    for decisions, TIMEOUT_CODEGEN=300s for teach-mode codegen. Timeout ->
    BrainTimeout; quota/rate-limit text -> QuotaExhausted.

Decision flow (both brains):

    transcript --> [build prompt: toolbox + skill manifest + transcript]
               --> model --> raw text
               --> extract_last_json_block --> validate_action
               --> action dict   (ContractError / BrainTimeout / QuotaExhausted
                                   surface to the caller, which toasts + falls
                                   back to the keyword matcher on quota)
"""
import json
import subprocess
import tempfile
from pathlib import Path

from .actions import TOOLBOX_PROMPT
from .jsonx import ContractError, extract_last_json_block, validate_action

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MOCK_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "mock_brain.json"

# Screen-referential triggers: only these (or a force flag) attach a
# screenshot to the prompt (OV-2 — text-only by default).
_SCREEN_REFERENTIAL = ("this", "on my screen", "what am i looking at", "on screen")

# Substrings that mean the free-tier model access is exhausted (Deep Review
# error row / Amendment 1). Matched case-insensitively against output+stderr.
_QUOTA_MARKERS = (
    "rate limit",
    "rate-limit",
    "quota",
    "usage limit",
    "too many requests",
    "429",
    "exceeded your current",
    "insufficient_quota",
)


class BrainError(Exception):
    """Base for brain failures."""


class BrainTimeout(BrainError):
    """The model call exceeded its timeout and was killed."""


class QuotaExhausted(BrainError):
    """The model access is rate/quota limited for this window."""


def build_prompt(transcript: str, context: dict | None = None) -> str:
    """Assemble the model prompt: toolbox + skill manifest placeholder +
    transcript. Kept in one place so MockBrain and CodexBrain agree on it.
    """
    context = context or {}
    manifest = context.get("skill_manifest", "(no learned skills yet)")
    return (
        "You are Miko, a desktop agent. Respond with ONE line of JSON only, "
        'matching {"action": ..., "args": {...}, "say": "<one-line toast>"}.\n\n'
        f"{TOOLBOX_PROMPT}\n\n"
        f"Learned skills (name: keywords):\n{manifest}\n\n"
        f"User said: {transcript!r}\n"
    )


def wants_screenshot(transcript: str, force: bool = False) -> bool:
    if force:
        return True
    low = (transcript or "").lower()
    return any(marker in low for marker in _SCREEN_REFERENTIAL)


class Brain:
    def decide(self, transcript: str, context: dict | None = None) -> dict:
        raise NotImplementedError


class MockBrain(Brain):
    """Scripted brain for dev/test. Never calls any real model."""

    def __init__(self, fixture_path=None):
        path = Path(fixture_path) if fixture_path else _DEFAULT_MOCK_FIXTURE
        data = json.loads(path.read_text(encoding="utf-8"))
        self._default = data.get("default")
        self._rules = data.get("rules", [])

    def decide(self, transcript: str, context: dict | None = None) -> dict:
        rule = self._match_rule(transcript)
        mode = rule.get("mode", "normal")

        if mode == "timeout":
            raise BrainTimeout("mock timeout")
        if mode == "quota":
            raise QuotaExhausted("mock quota exhausted")

        if mode == "no_json":
            # Model returned prose with no JSON -> parser yields None ->
            # ContractError, exactly like a real garbled response.
            raw = "Sorry, I can't help with that right now."
            return validate_action(extract_last_json_block(raw))

        if mode == "prose":
            # Wrap the canned JSON in chatter to exercise the extractor.
            payload = rule.get("response", self._default)
            raw = f"Sure! Here you go:\n{json.dumps(payload)}\nHope that helps."
            return validate_action(extract_last_json_block(raw))

        # normal: still route through validate_action so the contract is
        # enforced on the mock exactly as it would be on the real model.
        payload = rule.get("response", self._default)
        return validate_action(payload)

    def _match_rule(self, transcript: str) -> dict:
        low = (transcript or "").lower()
        for rule in self._rules:
            needle = rule.get("contains", "")
            if needle and needle.lower() in low:
                return rule
        return {"mode": "normal", "response": self._default}


class CodexBrain(Brain):
    """Real model bridge via the Codex CLI. NEVER invoked in tests against a
    real `codex` — tests override `_build_command`.
    """

    TIMEOUT_INTENT = 60
    TIMEOUT_CODEGEN = 300
    CODEX_CMD = ("codex", "exec")

    def decide(self, transcript: str, context: dict | None = None) -> dict:
        context = context or {}
        prompt = build_prompt(transcript, context)
        image = None
        if wants_screenshot(transcript, force=context.get("force_screenshot", False)):
            image = context.get("screenshot_path")  # captured by the caller
        raw = self._run(prompt, self.TIMEOUT_INTENT, image=image)
        return validate_action(extract_last_json_block(raw))

    def codegen(self, prompt: str) -> str:
        """Teach-mode path: longer timeout, returns the raw model output
        (the caller writes/loads the skill file)."""
        return self._run(prompt, self.TIMEOUT_CODEGEN, image=None)

    # -- internal --------------------------------------------------------

    def _build_command(self, prompt: str, output_file: str, image):
        cmd = list(self.CODEX_CMD) + [prompt, "-o", output_file]
        if image:
            cmd += ["--image", str(image)]
        return cmd

    def _run(self, prompt: str, timeout: int, image) -> str:
        with tempfile.NamedTemporaryFile(
            "w+", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            output_file = tf.name
        try:
            cmd = self._build_command(prompt, output_file, image)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                # subprocess.run already killed the child on timeout.
                raise BrainTimeout(f"codex exceeded {timeout}s and was killed")

            out = Path(output_file).read_text(encoding="utf-8", errors="replace") \
                if Path(output_file).exists() else ""
            combined = f"{out}\n{proc.stdout}\n{proc.stderr}".lower()
            if any(marker in combined for marker in _QUOTA_MARKERS):
                raise QuotaExhausted("codex reported a quota/rate limit")
            # -o file is the primary channel; fall back to stdout scraping.
            return out or proc.stdout
        finally:
            try:
                Path(output_file).unlink()
            except OSError:
                pass
