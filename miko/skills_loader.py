"""Teach-mode skill loader (design "Contracts & Mechanics" + Amendment 14).

Scans skills/*.py (skipping the template), statically rejects any skill that
imports beyond `miko.helpers` + stdlib `re`/`json` BEFORE importing it, then
imports inside try/except and validates the KEYWORDS + run(ctx) surface. A
broken skill triggers ONE automatic "here's the traceback, fix your code"
retry through brain.codegen (design doc line 56), then gives up gracefully.

Loaded skills feed two things: the manifest (name + keywords) included in the
GPT-5.6 prompt so it can pick action:"skill", and the DRY keyword matcher
(Amendment 5) used by the quota fallback. scan() is hot-reload cheap: only
new or mtime-changed files are re-imported on each wake.

The import policy is a FOOTGUN GUARD, not a security sandbox (Amendment 15):
it blocks the obvious ways a generated skill reaches outside the whitelist
(imports), not every conceivable runtime dodge.
"""
import ast
import importlib.util
import sys
from pathlib import Path

from . import helpers as _helpers_module
from .logs import log_event

# Bare module imports a skill may use.
_ALLOWED_IMPORT = {"re", "json", "miko.helpers"}
# `from X import ...` sources a skill may use (any names off these modules).
_ALLOWED_FROM = {"re", "json", "miko.helpers"}

_SKIP_FILES = {"_template.py", "__init__.py"}


class SkillLoadError(Exception):
    """A skill file failed to load (syntax, import, exec, or bad surface)."""


class SkillRejected(SkillLoadError):
    """A skill violated the static import policy (named reason)."""


class LoadedSkill:
    def __init__(self, name, keywords, run, mtime):
        self.name = name
        self.keywords = keywords
        self.run = run
        self.mtime = mtime


class SkillContext:
    """The `ctx` handed to a skill's run(): transcript + the helpers module."""

    def __init__(self, transcript, helpers):
        self.transcript = transcript
        self.helpers = helpers


def static_check(source: str, filename: str) -> None:
    """Reject a skill (before import) that violates the import policy.

    Raises SkillLoadError on a syntax error, SkillRejected (with a named
    reason) on a disallowed import.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise SkillLoadError(f"{filename}: syntax error: {exc}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _ALLOWED_IMPORT:
                    raise SkillRejected(
                        f"{filename}: disallowed import '{alias.name}' "
                        f"(allowed: {sorted(_ALLOWED_IMPORT)})"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                raise SkillRejected(f"{filename}: relative imports are not allowed")
            module = node.module or ""
            if module == "miko":
                bad = [a.name for a in node.names if a.name != "helpers"]
                if bad:
                    raise SkillRejected(
                        f"{filename}: 'from miko import {', '.join(bad)}' not allowed "
                        "(only 'helpers')"
                    )
            elif module not in _ALLOWED_FROM:
                raise SkillRejected(
                    f"{filename}: disallowed import 'from {module}' "
                    f"(allowed: {sorted(_ALLOWED_FROM | {'miko'})})"
                )


class SkillsLoader:
    def __init__(self, skills_dir="skills", brain=None, helpers=None):
        self.skills_dir = Path(skills_dir)
        self.brain = brain
        self._helpers = helpers or _helpers_module
        self.skills = {}   # name -> LoadedSkill
        self.errors = {}   # name -> reason string
        self._mtimes = {}  # str(path) -> mtime last acted on

    # -- public surface ---------------------------------------------------

    def scan(self):
        """Hot-reload: (re)load new/changed skills, drop deleted ones.

        Cheap by design: a file whose mtime is unchanged since we last acted
        on it is skipped. Returns the current manifest.
        """
        seen = set()
        for path in sorted(self.skills_dir.glob("*.py")):
            if path.name in _SKIP_FILES:
                continue
            seen.add(path.stem)
            key = str(path)
            mtime = path.stat().st_mtime
            already = path.stem in self.skills or path.stem in self.errors
            if already and self._mtimes.get(key) == mtime:
                continue  # unchanged since last scan -> no re-import
            try:
                # scan() runs on every wake + at startup; it must NEVER trigger
                # a codegen "fix" (that would spend Codex quota on a broken file
                # each wake). Only teach-mode may repair. (S2)
                self.load_path(path, allow_fix=False)
            except SkillLoadError as exc:
                self.errors[path.stem] = str(exc)
                self.skills.pop(path.stem, None)
                # Record the CURRENT mtime (post-retry rewrite) so we don't
                # re-attempt a known-bad file on every single wake.
                self._mtimes[key] = path.stat().st_mtime
                log_event("skill_load_failed", skill=path.stem, reason=str(exc))
        # forget skills whose files are gone
        for name in [n for n in self.skills if n not in seen]:
            del self.skills[name]
        for name in [n for n in self.errors if n not in seen]:
            del self.errors[name]
        return self.manifest()

    def load_path(self, path, behavior=None, allow_fix=True) -> LoadedSkill:
        """Static-check + import + validate one skill, with ONE codegen retry.

        Returns the LoadedSkill on success (and registers it). Raises
        SkillLoadError if it is still broken after the single retry.
        `allow_fix=False` disables the codegen repair entirely (used by scan()
        so a broken skill never spends quota on every wake -- only teach-mode
        repairs).
        """
        path = Path(path)
        try:
            skill = self._static_import_validate(path)
        except SkillLoadError as first_error:
            if self.brain is None or not allow_fix:
                raise
            log_event("skill_retry", skill=path.stem, reason=str(first_error))
            self._codegen_fix(path, first_error, behavior)
            skill = self._static_import_validate(path)  # may raise -> give up
        self.skills[skill.name] = skill
        self.errors.pop(skill.name, None)
        self._mtimes[str(path)] = path.stat().st_mtime
        return skill

    def manifest(self):
        """[(name, keywords), ...] for the loaded skills."""
        return [(s.name, s.keywords) for s in self.skills.values()]

    def manifest_prompt(self) -> str:
        """The manifest as a string for the GPT-5.6 prompt."""
        rows = [f"{name}: {', '.join(kw)}" for name, kw in self.manifest()]
        return "\n".join(rows) if rows else "(no learned skills yet)"

    def register_into(self, matcher) -> None:
        """Register loaded skills into the DRY matcher (keyword routing +
        quota fallback). Idempotent: re-registering a name overwrites it."""
        for name, keywords in self.manifest():
            matcher.register(
                name,
                keywords,
                {"action": "skill", "args": {"name": name}, "say": f"Running {name}."},
            )

    def run_skill(self, name, transcript) -> str:
        """Run a loaded skill by name; never raises past here."""
        skill = self.skills.get(name)
        if skill is None:
            return f"I haven't learned the skill '{name}' yet."
        ctx = SkillContext(transcript=transcript, helpers=self._helpers)
        try:
            result = skill.run(ctx)
        except Exception as exc:
            log_event("skill_run_error", skill=name, error=str(exc))
            return f"The skill '{name}' hit an error: {exc}"
        return result if isinstance(result, str) else f"Ran skill '{name}'."

    # -- internal ---------------------------------------------------------

    def _codegen_fix(self, path, error, behavior):
        from .teach import build_codegen_prompt  # local import: avoid cycle

        template = ""
        tpl = self.skills_dir / "_template.py"
        try:
            template = tpl.read_text(encoding="utf-8")
        except OSError:
            pass
        prompt = build_codegen_prompt(
            path, behavior or "(repair the existing skill)",
            template_text=template, previous_error=error,
        )
        self.brain.codegen(prompt)

    def _static_import_validate(self, path) -> LoadedSkill:
        source = path.read_text(encoding="utf-8")
        static_check(source, path.name)
        module = self._import_module(path)
        keywords = getattr(module, "KEYWORDS", None)
        if not (isinstance(keywords, list) and keywords
                and all(isinstance(k, str) for k in keywords)):
            raise SkillLoadError(
                f"{path.name}: KEYWORDS must be a non-empty list of strings"
            )
        run = getattr(module, "run", None)
        if not callable(run):
            raise SkillLoadError(f"{path.name}: missing callable run(ctx)")
        return LoadedSkill(
            name=path.stem, keywords=list(keywords), run=run,
            mtime=path.stat().st_mtime,
        )

    def _import_module(self, path):
        mod_name = f"miko_skill_{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except SyntaxError as exc:
            sys.modules.pop(mod_name, None)
            raise SkillLoadError(f"{path.name}: syntax error: {exc}")
        except Exception as exc:
            sys.modules.pop(mod_name, None)
            raise SkillLoadError(f"{path.name}: import failed: {exc}")
        return module
