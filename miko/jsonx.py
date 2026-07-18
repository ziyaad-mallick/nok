"""JSON extraction + action-contract validation.

`extract_last_json_block` was moved here from scripts/verify_codex.py so the
runtime pipeline and the probe share ONE parser (the `codex exec` harness
emits prose chatter around the JSON — OV-4). `validate_action` enforces the
GPT-5.6 JSON contract pinned in the design doc:

    {"action": <whitelist enum>, "args": <dict>, "say": <str>}

Anything else raises ContractError so the dispatcher can turn it into a
user-visible error toast instead of executing garbage.
"""
import json

# Single source of truth for the action whitelist (design doc "Contracts &
# Mechanics"). Imported by config, dispatcher, matcher and actions so the
# boundary is defined in exactly one place.
ACTION_WHITELIST = (
    "open_app",
    "close_app",
    "type_text",
    "media",
    "screenshot",
    "macro",
    "skill",
)


class ContractError(ValueError):
    """Raised when a model response does not satisfy the action contract."""


def extract_last_json_block(text: str):
    """Return the last valid top-level JSON object/array found in `text`,
    scanning left to right past leading prose and duplicate blocks.

    Returns None if no valid JSON object/array is found (including the
    case of truncated/incomplete JSON).
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    last = None
    i = 0
    n = len(text)
    while i < n:
        if text[i] in "{[":
            try:
                obj, end = decoder.raw_decode(text, i)
                last = obj
                i = end
                continue
            except json.JSONDecodeError:
                pass
        i += 1
    return last


def validate_action(obj) -> dict:
    """Validate `obj` against the action contract; return it unchanged on
    success, raise ContractError with a specific reason on failure.

    Contract: a dict with
      - "action": str in ACTION_WHITELIST
      - "args":   dict (defaults to {} if omitted)
      - "say":    str  (defaults to "" if omitted)
    """
    if not isinstance(obj, dict):
        raise ContractError(f"response is not a JSON object (got {type(obj).__name__})")

    action = obj.get("action")
    if not isinstance(action, str):
        raise ContractError("missing or non-string 'action'")
    if action not in ACTION_WHITELIST:
        raise ContractError(
            f"action '{action}' is not in the whitelist {ACTION_WHITELIST}"
        )

    # args/say are optional in the raw response but normalized to their empty
    # defaults so downstream code can assume the shape.
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise ContractError("'args' must be an object/dict")

    say = obj.get("say", "")
    if not isinstance(say, str):
        raise ContractError("'say' must be a string")

    obj["args"] = args
    obj["say"] = say
    return obj
