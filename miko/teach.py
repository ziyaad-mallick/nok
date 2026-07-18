"""Teach-mode flow: Codex writes Miko a new skill, live (design doc #3).

"Miko, learn a new trick: when I say focus mode, mute my audio and open my
notes." -> parse the described behavior -> hand Codex the filled-in template
plus an explicit target path -> Codex EDITS the template into skills/<slug>.py
(Amendment 14: edit-at-a-path, never create-from-prose) -> verify the file
appeared -> static-check + import via the loader -> on failure, ONE automatic
"here's the traceback, fix your code" retry -> speak the outcome either way.

The codegen call uses the 300s path (CodexBrain.codegen == TIMEOUT_CODEGEN);
in dev/test it runs against MockBrain.codegen, which writes a canned skill file
(and, in its misbehavior modes, a bad-import / broken-syntax file, or nothing).

The `SKILL_PATH=` marker line is load-bearing: it is how MockBrain knows where
to write, and how the real Codex is told the one path it may create.
"""
from pathlib import Path

from .logs import log_event

# Phrases that route a wake into teach mode instead of the dispatcher.
TEACH_TRIGGERS = ("learn a new trick", "teach you", "new trick", "learn to")

# Trigger phrases stripped to recover the described behavior (longest first so
# "learn a new trick" wins over "new trick").
_STRIP_TRIGGERS = ("learn a new trick", "teach you to", "teach you", "learn to", "new trick")

# The hard rules restated to the model (kept in sync with _template.py's docstring).
_CONTRACT_RULES = (
    "HARD RULES for the skill file:\n"
    "  1. Imports: ONLY `from miko import helpers` and stdlib `re` / `json`.\n"
    "  2. Define KEYWORDS: a non-empty list of lowercase strings for routing.\n"
    "  3. Define run(ctx) -> str: do the work, return a SHORT status string.\n"
    "  4. ctx.transcript is what the user said; ctx.helpers exposes open_app,\n"
    "     close_app, type_text, media, screenshot, sleep, get_active_window_title.\n"
    "  Edit the template body ONLY; keep the imports and the run(ctx) signature."
)

MAX_SLUG_TOKENS = 5


def is_teach_intent(transcript: str) -> bool:
    low = (transcript or "").lower()
    return any(t in low for t in TEACH_TRIGGERS)


def parse_learn_request(transcript: str) -> str:
    """Strip the trigger phrase and recover the described behavior."""
    text = transcript or ""
    low = text.lower()
    best_idx, best_trig = -1, None
    for trig in _STRIP_TRIGGERS:
        j = low.find(trig)
        if j != -1 and (best_idx == -1 or j < best_idx):
            best_idx, best_trig = j, trig
    if best_trig is None:
        return text.strip()
    rest = text[best_idx + len(best_trig):].lstrip(" :,-")
    return rest.strip()


def slugify(text: str) -> str:
    """Filesystem-safe skill stem from the described behavior."""
    tokens = []
    for raw in (text or "").lower().split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            tokens.append(cleaned)
        if len(tokens) >= MAX_SLUG_TOKENS:
            break
    return "_".join(tokens) or "skill"


def skill_path_marker(path) -> str:
    """The single line MockBrain parses and the real Codex is told to obey."""
    return f"SKILL_PATH={path}"


def build_codegen_prompt(target_path, behavior, template_text="", previous_error=None) -> str:
    """Assemble the teach-mode codegen prompt.

    Contains the contract rules, the filled-in template to edit, the explicit
    target path, and the user's described behavior. On a retry, the previous
    failure's traceback/reason is appended so the model can fix it.
    """
    parts = [
        "You are Codex, writing a new skill for Miko, a desktop agent.",
        _CONTRACT_RULES,
        "",
        "Create the file at EXACTLY the path on this marker line (do not pick "
        "another path or name):",
        skill_path_marker(target_path),
        "",
        "Start from this template and edit ONLY its body / KEYWORDS:",
        "----- TEMPLATE -----",
        template_text or "(template unavailable)",
        "----- END TEMPLATE -----",
        "",
        f"Desired behavior: {behavior}",
    ]
    if previous_error:
        parts += [
            "",
            "Your previous attempt failed to load. Here is the error; fix your "
            "code and write the corrected file to the same path:",
            str(previous_error),
        ]
    return "\n".join(parts)


def _read_template(skills_dir) -> str:
    tpl = Path(skills_dir) / "_template.py"
    try:
        return tpl.read_text(encoding="utf-8")
    except OSError:
        return ""


def teach(transcript: str, brain, loader, skills_dir) -> str:
    """Run the full teach-mode flow. Returns a user-facing say string."""
    skills_dir = Path(skills_dir)
    behavior = parse_learn_request(transcript)
    if not behavior:
        return "Tell me what the new trick should do."

    slug = slugify(behavior)
    target = skills_dir / f"{slug}.py"
    template_text = _read_template(skills_dir)

    log_event("teach_start", transcript=transcript, behavior=behavior, target=str(target))

    prompt = build_codegen_prompt(target, behavior, template_text=template_text)
    brain.codegen(prompt)  # CodexBrain: 300s path; MockBrain: writes the file

    # Explicit-path verification (Amendment 14): the file must appear where we
    # told the model to write it — nowhere else counts.
    if not target.exists():
        log_event("teach_no_file", target=str(target))
        return f"I tried to learn '{behavior}', but no skill file was written."

    # Static check + import, with ONE automatic traceback retry (the loader
    # owns both the check and the single-retry, so teach and hot-reload agree).
    try:
        skill = loader.load_path(target, behavior=behavior)
    except Exception as exc:  # loader raises SkillLoadError after the retry
        log_event("teach_failed", target=str(target), reason=str(exc))
        return f"I couldn't learn '{behavior}': {exc}"

    log_event("teach_ok", skill=skill.name, keywords=skill.keywords)
    kw = ", ".join(skill.keywords)
    return f"Learned '{skill.name}'. Say something with: {kw}."
