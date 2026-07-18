"""TEMPLATE SKILL — Codex edits a COPY of this file at an explicit path.

Teach mode never asks the model to write a skill from a blank page: it hands
Codex this filled-in, working example and asks it to change the body. Keep the
shape below EXACTLY; the loader enforces these hard rules before your skill
will load, and rejects the file (with a named reason) if any is broken:

  1. IMPORTS: only `from miko import helpers` and stdlib `re` / `json`.
     Any other import is rejected statically, before the file is executed.
  2. KEYWORDS: a non-empty list of lowercase strings. Miko routes a spoken
     command to this skill when its words hit these keywords.
  3. run(ctx) -> str: do the work, then return a SHORT status string. The
     string is spoken to the user and written to the session log.
  4. ctx carries exactly two things:
       ctx.transcript -> str   : what the user said this wake
       ctx.helpers    -> the miko.helpers module, with:
           open_app(name), close_app(name), type_text(text),
           media(command), screenshot(), sleep(seconds),
           get_active_window_title()

Do all work through ctx.helpers (or `helpers` imported above). Never import,
open, or call anything else — the loader will refuse the skill.
"""
from miko import helpers  # noqa: F401  (kept so edited skills can use it directly)

KEYWORDS = ["example", "template", "demo"]


def run(ctx) -> str:
    # Example behavior: mute audio, then type a short note. Replace this body
    # with what the user asked for — keep the imports and the signature.
    ctx.helpers.media("mute")
    ctx.helpers.type_text("hello from the template skill")
    return "Ran the template skill."
