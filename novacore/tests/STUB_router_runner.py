"""STUB router runner for the N1 tests: scripted keyword answers. NOT A MODEL.

It stands in for a grammar-limited model runner such as llama.cpp's
`llama-completion --json-schema-file`. It reads the router prompt on stdin,
takes the last "Owner: " line, and answers by keyword. Like a grammar-limited
runner, it only names actions that appear as "const" values in the schema file
given as its first argument, and it ends with llama.cpp's end-of-text trailer.
It says STUB on stderr every time it runs (AGENTS.md rule 9).
"""

from __future__ import annotations

import json
import sys


def _action(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"route": "action", "capability": name, "args": arguments}


def main() -> None:
    print("STUB router runner: scripted keyword answers, not a model", file=sys.stderr)
    with open(sys.argv[1], encoding="utf-8") as handle:
        schema = json.load(handle)
    offered = {branch["properties"]["capability"]["const"] for branch in schema["oneOf"][1:]}
    owner_lines = [line for line in sys.stdin.read().splitlines() if line.startswith("Owner: ")]
    words = owner_lines[-1][len("Owner: "):]
    lowered = words.casefold()
    answer: dict[str, object] = {"route": "chat"}
    if lowered.startswith("what time"):
        answer = _action("time.now", {})
    elif lowered.startswith("find "):
        answer = _action("files.find", {"query": words[len("find "):]})
    elif lowered.startswith("remember "):
        answer = _action("memory.save", {"text": words[len("remember "):]})
    elif lowered.startswith("start "):
        answer = _action("apps.open", {"app": words[len("start "):]})
    if answer.get("capability", None) not in (None, *offered):
        answer = {"route": "chat"}
    print(json.dumps(answer) + " [end of text]")


if __name__ == "__main__":
    main()
