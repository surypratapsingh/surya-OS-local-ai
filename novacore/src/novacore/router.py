"""Work order N1: turn the owner's words into at most one proposed action.

The router is a second, small model call with one job: given the owner's words
and the list of actions this computer offers, answer `{"route": "chat"}` or one
action with its arguments. It proposes; the broker decides.

What keeps this safe does not depend on the model behaving:

1. `router_prompt()` takes the owner's words and the offered actions and
   nothing else. Recalled notes, the conversation history and imported files
   are untrusted (SCOPE.md) and cannot reach the call that picks an action:
   no code path hands them to it.
2. `request_schema()` describes every valid answer as JSON Schema, for a runner
   that limits sampling to it (llama.cpp's `--json-schema-file`). A model run
   under that grammar cannot spell an action that is not offered.
3. `parse_route()` does not trust the runner either. It accepts one JSON object
   and nothing around it, no duplicate keys, no NaN or Infinity, exactly the
   expected keys, an offered action, and arguments inside their rules. Any
   other output is not an action: the owner's words go to chat instead.

The broker re-checks the arguments before anything runs
(`capabilities.argument_problem`), and owner confirmation happens outside the
model entirely (`assistant.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import uuid

from .capabilities import ArgSpec, argument_problem
from .llm import ModelBackend, ModelUnavailable


CHAT_ROUTE = "chat"
ACTION_ROUTE = "action"

# llama.cpp's completion tool prints this literal on stdout when generation
# ends on an end-of-generation token (tools/completion/completion.cpp in the
# llama.cpp tree read on 2026-10-31: `LOG(" [end of text]\n")`; LOG writes
# to stdout). Only this exact trailer is removed; nothing else is skipped.
END_OF_TEXT = "[end of text]"

# The owner's words are clipped before they reach the router; a command is
# short. Longer text still reaches chat in full.
MAX_OWNER_CHARS = 500
# A valid answer is far shorter than this; a longer output is not parsed.
MAX_OUTPUT_CHARS = 2_000


@dataclass(frozen=True)
class ActionSpec:
    """One action as the router is allowed to offer it."""

    name: str
    description: str
    args: Mapping[str, ArgSpec]
    example_words: str
    example_args: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    """The router's decision. `note` says why an output was not an action."""

    kind: str
    capability: str | None = None
    args: Mapping[str, object] | None = None
    note: str = ""


def _chat(note: str = "") -> Route:
    return Route(CHAT_ROUTE, note=note)


def owner_words(text: str) -> str:
    """Collapse whitespace (so the words stay on one prompt line) and clip."""
    return " ".join(text.split())[:MAX_OWNER_CHARS]


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _action_json(name: str, args: Mapping[str, object]) -> str:
    return _compact({"route": ACTION_ROUTE, "capability": name, "args": dict(args)})


def router_prompt(owner_text: str, actions: Sequence[ActionSpec]) -> str:
    """The whole router prompt. Its inputs are the owner's words and the actions."""
    lines = [
        "You turn the owner's words into exactly one JSON object. You only choose; "
        "you never run anything.",
        "If the words ask for one of the actions below, answer with that action and "
        "its arguments.",
        'For anything else (a question, conversation, an action not in the list, or '
        f'words you are unsure about) answer {_compact({"route": CHAT_ROUTE})}.',
        "",
        "Actions:",
    ]
    for action in actions:
        if action.args:
            arguments = "; ".join(f"{key}: {spec.describe()}" for key, spec in action.args.items())
        else:
            arguments = "none"
        lines.append(f"{action.name} - {action.description} Arguments: {arguments}.")
    lines += ["", "Examples:"]
    for action in actions:
        lines.append(f"Owner: {action.example_words}")
        lines.append(f"JSON: {_action_json(action.name, action.example_args)}")
    lines.append("Owner: what is the boiling point of water?")
    lines.append(f"JSON: {_compact({'route': CHAT_ROUTE})}")
    lines.append("Owner: thank you, that helped")
    lines.append(f"JSON: {_compact({'route': CHAT_ROUTE})}")
    lines += ["", f"Owner: {owner_words(owner_text)}", "JSON:"]
    return "\n".join(lines)


def request_schema(actions: Sequence[ActionSpec]) -> dict[str, object]:
    """JSON Schema of every valid router answer: chat, or one offered action."""
    branches: list[dict[str, object]] = [
        {
            "type": "object",
            "properties": {"route": {"const": CHAT_ROUTE}},
            "required": ["route"],
            "additionalProperties": False,
        }
    ]
    for action in actions:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "route": {"const": ACTION_ROUTE},
                    "capability": {"const": action.name},
                    "args": {
                        "type": "object",
                        "properties": {key: spec.schema() for key, spec in action.args.items()},
                        "required": list(action.args),
                        "additionalProperties": False,
                    },
                },
                "required": ["route", "capability", "args"],
                "additionalProperties": False,
            }
        )
    return {"oneOf": branches}


def write_schema_file(directory: Path, actions: Sequence[ActionSpec]) -> Path:
    """Write `request_schema(actions)` for the runner's `{schema_file}` argument.

    The schema holds the action names and the owner's application names: it
    comes from configuration, not conversation. It is still written owner-only
    (0o700 folder, 0o600 file on POSIX) and replaced atomically.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / "schema.json"
    temporary = directory / f".schema-{uuid.uuid4().hex}.tmp"
    data = json.dumps(request_schema(actions), ensure_ascii=False, indent=1).encode("utf-8")
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


class _Rejected(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _Rejected(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constants(name: str) -> object:
    raise _Rejected(f"{name} is not JSON")


_DECODER = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
_TRAILER = re.compile(r"\s*" + re.escape(END_OF_TEXT) + r"\s*\Z")


def _one_object(output: str) -> dict[str, object]:
    text = _TRAILER.sub("", output).strip()
    if not text.startswith("{"):
        raise _Rejected("output does not start with a JSON object")
    try:
        value, end = _DECODER.raw_decode(text)
    except _Rejected:
        raise
    except json.JSONDecodeError as error:
        raise _Rejected(f"not valid JSON ({error.msg})") from error
    except (ValueError, RecursionError) as error:
        # RecursionError: "[[[[..." nested past the interpreter's limit. A
        # backstop: under MAX_OUTPUT_CHARS, CPython 3.13 parses every nesting
        # depth that fits (measured 2026-10-31: 997 levels parse; 100,000 raise).
        raise _Rejected(f"not valid JSON ({type(error).__name__})") from error
    if end != len(text):
        raise _Rejected("text follows the JSON object")
    if not isinstance(value, dict):
        raise _Rejected("not a JSON object")
    return value


def parse_route(output: str, actions: Mapping[str, ActionSpec]) -> Route:
    """Read a runner's output strictly. Anything that is not exactly valid is chat."""
    if len(output) > MAX_OUTPUT_CHARS:
        return _chat(f"router output longer than {MAX_OUTPUT_CHARS} characters")
    try:
        value = _one_object(output)
    except _Rejected as error:
        return _chat(f"router output rejected: {error}")
    if set(value) == {"route"} and value["route"] == CHAT_ROUTE:
        return _chat()
    if set(value) != {"route", "capability", "args"}:
        return _chat("router output rejected: unexpected keys " + ", ".join(sorted(value)))
    if value["route"] != ACTION_ROUTE:
        return _chat("router output rejected: route is neither chat nor action")
    name = value["capability"]
    if not isinstance(name, str) or name not in actions:
        return _chat("router output rejected: not an offered action")
    problem = argument_problem(actions[name].args, value["args"])
    if problem is not None:
        return _chat(f"router output rejected: {name} {problem}")
    return Route(ACTION_ROUTE, name, dict(value["args"]))


class Router:
    """Asks the router runner to pick, then parses its answer strictly.

    It holds the runner and the offered actions only: there is no memory store,
    history or file reader here for an injected instruction to come from.
    """

    def __init__(self, backend: ModelBackend, actions: Sequence[ActionSpec]) -> None:
        self.backend = backend
        self.actions = tuple(actions)
        self._by_name = {action.name: action for action in self.actions}

    def route(self, owner_text: str) -> Route:
        if not self.actions:
            return _chat()
        try:
            output = self.backend.complete(router_prompt(owner_text, self.actions))
        except ModelUnavailable as error:
            return _chat(f"router unavailable: {error}")
        return parse_route(output, self._by_name)
