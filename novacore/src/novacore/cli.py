"""Human-operated CLI. Model text is never parsed into commands or capabilities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .capabilities import CATALOGUE, CapabilityBroker
from .companion import Companion
from .config import ConfigError, RuntimeConfig, load_config
from .demand import DemandStore
from .llm import LocalCommandBackend
from .memory import MemoryStore
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
  /demand [list|add|export|show|resolve] ...
                             manage or export offline shuttle demand entries
  /quit

Any other text is sent to the configured local model runner. The model cannot
turn its own text into commands, hardware actions, saved memories, or updates.
"""


def _runtime(config: RuntimeConfig) -> tuple[Companion, CapabilityBroker, UpdateProposalStore, DemandStore]:
    memory = MemoryStore(config.state_dir / "memories.jsonl")
    companion = Companion(
        LocalCommandBackend(config.model, config.state_dir),
        memory,
        config.conversation,
    )
    broker = CapabilityBroker(config.state_dir / "events.jsonl")
    proposals = UpdateProposalStore(config.state_dir / "proposals")
    demands = DemandStore(config.state_dir / "demands.jsonl")
    return companion, broker, proposals, demands


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
    demands: DemandStore | None = None,
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
            f"{name}: available_now={capability.available_now}, risk={capability.risk.value}; "
            f"{capability.description}"
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
    if command == "demand":
        if demands is None:
            raise ValueError("demand store is not available")
        subcmd, _, rest = argument.partition(" ")
        subcmd = subcmd.strip().casefold()
        rest = rest.strip()
        if not subcmd or subcmd == "list":
            status_filter = None if rest == "all" else "open"
            entries = demands.list(status=status_filter)
            if not entries:
                return "No open demands." if status_filter == "open" else "No demands recorded."
            return "\n".join(
                f"{e.entry_id} [{e.status.upper()}]: {e.title} ({e.item_type})"
                for e in entries
            )
        if subcmd == "export":
            if not rest:
                raise ValueError("usage: /demand export <directory>")
            target_path = Path(rest)
            md_path, json_path = demands.export(target_path)
            count = len(demands.list(status="open"))
            return f"Exported {count} open demand(s) to {target_path} ({md_path.name}, {json_path.name})."
        if subcmd == "show":
            if not rest:
                raise ValueError("usage: /demand show <demand_id>")
            entry = demands.get(rest)
            if entry is None:
                return f"Demand not found: {rest}"
            return entry.render_markdown()
        if subcmd == "resolve":
            if not rest:
                raise ValueError("usage: /demand resolve <demand_id>")
            if demands.resolve(rest):
                return f"Demand {rest} marked as fulfilled."
            return f"Demand not found: {rest}"
        if subcmd == "add":
            parts = [p.strip() for p in rest.split("|")]
            if len(parts) < 4:
                raise ValueError("usage: /demand add TITLE | DESCRIPTION | URL1,URL2 | STEP1; STEP2")
            title, desc, urls_str, steps_str = parts[0], parts[1], parts[2], parts[3]
            urls = [u.strip() for u in urls_str.split(",") if u.strip()]
            steps = [s.strip() for s in steps_str.split(";") if s.strip()]
            entry = demands.add(title=title, description=desc, urls=urls, instructions=steps)
            return f"Created demand entry {entry.entry_id}: {entry.title}"
        raise ValueError(f"unknown /demand subcommand: {subcmd}; use list, add, export, show, resolve")
    raise ValueError(f"unknown command: /{command}; use /help")


def _run_line(
    line: str,
    companion: Companion,
    broker: CapabilityBroker,
    proposals: UpdateProposalStore,
    demands: DemandStore | None = None,
) -> str:
    if line.startswith("/"):
        return _handle_command(line, companion, broker, proposals, demands)
    return companion.reply(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NOVA local companion runtime")
    parser.add_argument("--config", required=True, type=Path, help="owner-created local configuration TOML")
    parser.add_argument("--message", help="send one text message and exit")
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        companion, broker, proposals, demands = _runtime(config)
    except ConfigError as error:
        print(f"novacore: configuration error: {error}", file=sys.stderr)
        return 2

    if arguments.message is not None:
        try:
            print(_run_line(arguments.message, companion, broker, proposals, demands))
        except ValueError as error:
            print(f"novacore: {error}", file=sys.stderr)
            return 2
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
            print(_run_line(line, companion, broker, proposals, demands))
        except ValueError as error:
            print(f"novacore: {error}")
