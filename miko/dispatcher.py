"""Whitelisted action dispatcher.

Takes a VALIDATED action dict and runs it. Defense in depth: even though the
brain already validated the contract, the dispatcher re-checks the enum here
(the whitelist boundary moves for no one). Every dispatch is logged; every
error path returns a user-visible result string rather than raising past the
dispatcher.

Flow:

    dispatch(action)                      # public entry
      |
      +-- BUSY gate (non-reentrant): if a dispatch is already running on
      |   another thread -> drop this one, "still working" result
      |
      +-- _execute(action)                # also the macro step-runner
            |
            +-- enum check (not whitelisted -> error result)
            +-- route to Actions.<method> / skill_runner
            +-- catch any Exception -> error result string
            +-- log_event("dispatch", ...)
"""
import threading

from .jsonx import ACTION_WHITELIST
from .logs import log_event


class Dispatcher:
    def __init__(self, actions, skill_runner=None):
        """`actions` is a miko.actions.Actions; `skill_runner(name) -> str`
        is the teach-mode loader hook (built by the other work stream). If
        None, `skill` actions return a graceful error result.
        """
        self._actions = actions
        self._skill_runner = skill_runner
        self._busy = threading.Lock()

    def dispatch(self, action: dict) -> str:
        # BUSY handling hook: knocks/commands arriving while we're mid-dispatch
        # are dropped with one notice (design "BUSY state").
        if not self._busy.acquire(blocking=False):
            log_event("dispatch_busy_drop", action=action.get("action"))
            return "Still working on the last thing."
        try:
            return self._execute(action)
        finally:
            self._busy.release()

    # _execute is the shared executor: the public dispatch() and macro steps
    # both go through it, so macro steps re-enter the SAME whitelist check
    # without re-taking the (non-reentrant) BUSY lock.
    def _execute(self, action: dict) -> str:
        name = action.get("action")
        args = action.get("args", {}) or {}

        if name not in ACTION_WHITELIST:
            result = f"'{name}' is not an allowed action."
            log_event("dispatch", action=name, ok=False, result=result)
            return result

        try:
            result = self._route(name, args)
            ok = True
        except Exception as exc:  # never let an action crash the pipeline
            result = f"Something went wrong running {name}: {exc}"
            ok = False

        log_event("dispatch", action=name, args=args, ok=ok, result=result)
        return result

    def _route(self, name: str, args: dict) -> str:
        a = self._actions
        if name == "open_app":
            return a.open_app(args)
        if name == "close_app":
            return a.close_app(args)
        if name == "type_text":
            return a.type_text(args)
        if name == "media":
            return a.media(args)
        if name == "screenshot":
            return a.screenshot(args)
        if name == "macro":
            return a.run_macro(args, step_runner=self._execute)
        if name == "skill":
            if self._skill_runner is None:
                return f"I haven't learned the skill '{args.get('name')}' yet."
            return self._skill_runner(args.get("name"))
        # unreachable: enum was checked in _execute
        return f"'{name}' is not wired up."
