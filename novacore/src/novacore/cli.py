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
from .updates import UpdateProposalStore


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
  /quit

Any other text: with a [router] configured, the router may propose one
desktop action listed by /capabilities as "runs here". It runs only after the
broker checks it, and only after you type yes when it needs confirmation.
Everything else goes to the local model as chat. The chat model cannot turn
its own text into commands, hardware actions, saved memories, or updates.
"""


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


def _handle_command(
    raw: str,
    companion: Companion,
    broker: CapabilityBroker,
    proposals: UpdateProposalStore,
) -> str:
    command, _, argument = raw[1:].partition(" ")
    command = command.casefold()
    argument = argument.strip()
    if command == "help":
        return HELP
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
        return note + _handle_command(line, assistant.companion, assistant.broker, proposals)
    return assistant.handle(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NOVA local companion runtime")
    parser.add_argument("--config", required=True, type=Path, help="owner-created local configuration TOML")
    parser.add_argument("--message", help="send one text message and exit")
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        assistant, proposals = _runtime(config)
    except ConfigError as error:
        print(f"novacore: configuration error: {error}", file=sys.stderr)
        return 2

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
