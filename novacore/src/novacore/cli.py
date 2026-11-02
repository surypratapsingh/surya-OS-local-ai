"""Human-operated CLI.

Chat model text is never parsed into commands or capabilities. With a
[router] configured (work order N1), the router's strictly parsed answer may
propose one listed desktop action; the broker and the owner decide.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from .assistant import Assistant
from .capabilities import CATALOGUE, CapabilityBroker
from .companion import Companion
from .config import SCHEMA_FILE_TOKEN, ConfigError, RuntimeConfig, load_config
from .desktop import DesktopBridge, host_platform
from .llm import LocalCommandBackend
from .memory import MemoryStore
from .router import Router, write_schema_file
from .teaching import TeachingStore, training_lines
from .updates import UpdateProposalStore
from .voice import VoiceLoop


HELP = """Commands:
  /help
  /remember TEXT             persist an owner-approved text note
  /mem QUERY                 search saved notes
  /status                    inspect the safe bridge status
  /capabilities              list future capability names and their status
  /request NAME JSON         create an unconfirmed STUB capability event
  /confirm NAME JSON         explicitly confirm a capability request; still STUB
  /speak TEXT                create a STUB text-to-speech request
  /propose-update TITLE | SUMMARY
                             write a proposed_unverified update review document
  /teach WORDS => chat       with [learning] on: answer these words as chat
  /teach WORDS => CAPABILITY JSON
                             with [learning] on: these words mean this action
  /lessons                   list what NOVA has learned from you
  /quit

Any other text: with a [router] configured, the router may propose one
desktop action listed by /capabilities as "runs here". It runs only after the
broker checks it, and only after you type yes when it needs confirmation.
Everything else goes to the local model as chat. The chat model cannot turn
its own text into commands, hardware actions, saved memories, or updates.

With [learning] on, your yes or no to a proposed action is kept as a lesson
for those exact words (novacore/src/novacore/teaching.py).
"""

# Files made from lessons hold the owner's own commands. Their names must end
# in these, which .gitignore lists, so they are not committed by accident.
LESSON_EXPORT_SUFFIX = ".lessons.tsv"


def _runtime(config: RuntimeConfig) -> tuple[Assistant, UpdateProposalStore]:
    memory = MemoryStore(config.state_dir / "memories.jsonl")
    companion = Companion(
        LocalCommandBackend(config.model, config.state_dir),
        memory,
        config.conversation,
    )
    bridge = None
    if config.desktop is not None:
        bridge = DesktopBridge(config.desktop, host_platform(), memory=memory)
    broker = CapabilityBroker(config.state_dir / "events.jsonl", bridge)
    router = None
    if config.router is not None:
        actions = bridge.action_specs() if bridge is not None else []
        schema_path = write_schema_file(config.state_dir / "router", actions)
        command = tuple(str(schema_path) if part == SCHEMA_FILE_TOKEN else part for part in config.router.command)
        router = Router(LocalCommandBackend(replace(config.router, command=command), config.state_dir), actions)
    assistant = Assistant(
        companion,
        broker,
        router,
        bridge,
        confirm_all_actions=config.desktop.confirm_all_actions if config.desktop is not None else True,
        teaching=TeachingStore(config.state_dir / "lessons.jsonl") if config.learning.enabled else None,
        trust_learned=config.learning.trust_learned,
    )
    proposals = UpdateProposalStore(config.state_dir / "proposals")
    return assistant, proposals


def _json_object(text: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"arguments must be JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


def _teach(argument: str, assistant: Assistant) -> str:
    usage = "usage: /teach WORDS => chat, or /teach WORDS => CAPABILITY {\"argument\": \"value\"}"
    words, separator, target = argument.partition("=>")
    words, target = words.strip(), target.strip()
    if not separator or not words or not target:
        raise ValueError(usage)
    if target.casefold() == "chat":
        return assistant.teach(words, None, None)
    name, _, arguments_text = target.partition(" ")
    arguments = _json_object(arguments_text) if arguments_text.strip() else {}
    return assistant.teach(words, name, arguments)


def _handle_command(
    raw: str,
    companion: Companion,
    broker: CapabilityBroker,
    proposals: UpdateProposalStore,
    assistant: Assistant | None = None,
) -> str:
    command, _, argument = raw[1:].partition(" ")
    command = command.casefold()
    argument = argument.strip()
    if command == "help":
        return HELP
    if command in {"teach", "lessons"}:
        if assistant is None:
            raise ValueError(f"/{command} needs the assistant")
        return _teach(argument, assistant) if command == "teach" else assistant.lessons_report()
    if command == "remember":
        memory = companion.remember(argument)
        return f"Saved memory {memory.memory_id}."
    if command == "mem":
        matches = companion.memory.search(argument, limit=10)
        if not matches:
            return "No matching saved memories."
        return "\n".join(f"{memory.memory_id}: {memory.text}" for memory in matches)
    if command == "status":
        return broker.status().message
    if command == "capabilities":
        return "\n".join(
            f"{name}: available_now={capability.available_now}, risk={capability.risk.value}"
            + ("; runs here" if broker.runnable(name) else "")
            + ("; needs your typed yes" if capability.requires_confirmation else "")
            + f"; {capability.description}"
            for name, capability in CATALOGUE.items()
        )
    if command in {"request", "confirm"}:
        name, separator, arguments_text = argument.partition(" ")
        if not name or not separator:
            raise ValueError(f"usage: /{command} CAPABILITY {{\"argument\": \"value\"}}")
        result = broker.request(
            name,
            _json_object(arguments_text),
            owner_confirmed=command == "confirm",
        )
        return f"{result.state}: {result.message}" + (f" ({result.event_id})" if result.event_id else "")
    if command == "speak":
        result = broker.request("audio.speak", {"text": argument})
        return f"{result.state}: {result.message}" + (f" ({result.event_id})" if result.event_id else "")
    if command == "propose-update":
        title, separator, summary = argument.partition("|")
        if not separator:
            raise ValueError("usage: /propose-update TITLE | SUMMARY")
        path = proposals.create(title, summary)
        return f"proposed_unverified: wrote {path}; no update was applied."
    raise ValueError(f"unknown command: /{command}; use /help")


def _run_line(line: str, assistant: Assistant, proposals: UpdateProposalStore) -> str:
    if line.startswith("/"):
        # A slash command is the owner's own instruction: it cancels a waiting
        # action (it is not a "yes") and then runs as a command.
        note = ""
        if assistant.pending is not None:
            assistant.pending = None
            note = "Cancelled the waiting action; nothing was done.\n"
        return note + _handle_command(line, assistant.companion, assistant.broker, proposals, assistant)
    return assistant.handle(line)


def _export_lessons(log: Path, out: Path) -> int:
    """--export-lessons (N4d): the "do" and "chat" lessons as training lines for
    `intent-train train --extra` (intent/README.md). Never overwrites a file."""
    if not out.name.endswith(LESSON_EXPORT_SUFFIX):
        print(f"novacore: the export file name must end in {LESSON_EXPORT_SUFFIX}; .gitignore lists "
              f"those, so your commands are not committed by accident", file=sys.stderr)
        return 2
    try:
        lessons = TeachingStore(log).all()
    except ValueError as error:
        print(f"novacore: {error}", file=sys.stderr)
        return 2
    lines, left_out = training_lines(lessons)
    for lesson, reason in left_out:
        print(f'not written: "{lesson.text}" ({lesson.verdict}): {reason}')
    if not lines:
        print(f"novacore: no lesson in {log} makes a training line; nothing was written", file=sys.stderr)
        return 1
    try:
        with out.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(line + "\n" for line in lines))
    except FileExistsError:
        print(f"novacore: {out} already exists; nothing was overwritten", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"novacore: cannot write {out}: {error}", file=sys.stderr)
        return 2
    print(f"wrote {len(lines)} training line{'s' if len(lines) != 1 else ''} from "
          f"{len(lessons)} lesson{'s' if len(lessons) != 1 else ''} to {out}")
    print("This file holds your own commands. Keep it on this computer.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NOVA local companion runtime")
    parser.add_argument("--config", required=True, type=Path, help="owner-created local configuration TOML")
    parser.add_argument("--message", help="send one text message and exit")
    parser.add_argument("--voice", action="store_true", help="always-on voice from the [voice] table (N2)")
    parser.add_argument("--export-lessons", type=Path, metavar="FILE" + LESSON_EXPORT_SUFFIX,
                        help="write your lessons as training lines for intent-train --extra (N4d)")
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.export_lessons is not None:
            return _export_lessons(config.state_dir / "lessons.jsonl", arguments.export_lessons)
        assistant, proposals = _runtime(config)
    except ConfigError as error:
        print(f"novacore: configuration error: {error}", file=sys.stderr)
        return 2

    if arguments.voice:
        if config.voice is None:
            print("novacore: configuration error: --voice needs a [voice] table", file=sys.stderr)
            return 2

        def handle(command: str) -> str:
            try:
                return _run_line(command, assistant, proposals)
            except ValueError as error:
                return f"novacore: {error}"

        return VoiceLoop(
            handle,
            lambda: assistant.pending is not None,
            config.voice.listener,
            config.voice.speaker,
            config.voice.wake_words,
        ).run()

    if arguments.message is not None:
        try:
            print(_run_line(arguments.message, assistant, proposals))
        except ValueError as error:
            print(f"novacore: {error}", file=sys.stderr)
            return 2
        if assistant.pending is not None:
            assistant.pending = None
            print("A one-shot run cannot take your confirmation, so nothing was done. "
                  "Run NOVA without --message, or use /confirm NAME JSON.")
        return 0

    print("NOVA local companion. Type /help. Type /quit to exit.")
    while True:
        try:
            line = input("nova> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.casefold() in {"/quit", "/exit"}:
            return 0
        if not line:
            continue
        try:
            print(_run_line(line, assistant, proposals))
        except ValueError as error:
            print(f"novacore: {error}")
