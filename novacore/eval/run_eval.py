"""Work order N3: score a router on the hand-written command set.

    PYTHONPATH=novacore/src python3 novacore/eval/run_eval.py --config novacore/config.toml
        [--set novacore/eval/commands.jsonl] [--apps novacore/eval/apps.json]
        [--transcript FILE] [--limit N]

It uses the `[router]` table of the owner's configuration as it is: the same
runner command NovaCore itself would start. The rest is fixed for the eval:
every desktop action is offered, with the applications in apps.json. Each
line goes the way it goes in normal use, `router_prompt` -> the runner ->
`parse_route` (the N1 strict parser), and the answer is only compared with
the expected one. Nothing is run: the eval desktop has no platform to act on.

It prints, per capability, how many lines were right; the false actions (an
action where chat was right), wrong actions (another action, or the right one
with other arguments), missed actions (chat where an action was right), the
outputs the strict parser refused, and the seconds per line. The gate
(docs/work-orders.md N3): no wrong and no false action on a capability that
runs without confirmation (every one but memory.save). A router that answers
chat to everything meets that gate and is useless, so compare the right
counts too. If the runner itself fails on any line (not found, crashed, timed
out), the run measures nothing: it is reported as invalid, never as a pass.
Exit status: 0 gate met, 1 not met, 2 invalid run, usage or configuration
error.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

from novacore.capabilities import CATALOGUE
from novacore.config import SCHEMA_FILE_TOKEN, ConfigError, DesktopConfig, load_config
from novacore.desktop import DesktopBridge
from novacore.intent import same_answer
from novacore.llm import LocalCommandBackend
from novacore.memory import MemoryStore
from novacore.router import CHAT_ROUTE, Router, write_schema_file

HERE = Path(__file__).resolve().parent
RUNNER_FAILED = "router unavailable"  # the note Router.route() gives when the runner fails (router.py)


class NoPlatform:
    """The eval desktop's platform. The eval never runs an action; if it ever did, this stops it."""

    name = "eval (acts on nothing)"

    def __getattr__(self, name: str):
        raise RuntimeError(f"the N3 eval never acts (asked for {name})")


def load_rows(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        row = json.loads(line)
        if row.get("route") not in ("chat", "action") or not isinstance(row.get("text"), str):
            raise ValueError(f"{path}:{number}: each line needs text and route")
        if row["route"] == "action" and (row.get("capability") not in CATALOGUE or not isinstance(row.get("args"), dict)):
            raise ValueError(f"{path}:{number}: an action line needs a known capability and args")
        rows.append(row)
    return rows


def asks_first(capability: str) -> bool:
    return CATALOGUE[capability].requires_confirmation


def score(rows: list[dict], answers: list[tuple[dict, str, float]]) -> dict[str, object]:
    """Compare each answer with its line. `answers`: (answer, parser note, seconds) per line."""
    per_capability: Counter[str] = Counter()
    right_per_capability: Counter[str] = Counter()
    outcomes = Counter()
    gate = 0
    lines = []
    for row, (got, note, seconds) in zip(rows, answers, strict=True):
        expected = {k: row[k] for k in ("route", "capability", "args") if k in row}
        kind = row.get("capability", "chat")
        per_capability[kind] += 1
        if same_answer(expected, got):
            outcome = "right"
            right_per_capability[kind] += 1
        elif got["route"] == CHAT_ROUTE:
            outcome = "missed"
        elif expected["route"] == CHAT_ROUTE:
            outcome = "FALSE ACTION"
        else:
            outcome = "WRONG ACTION"
        if outcome in ("FALSE ACTION", "WRONG ACTION") and not asks_first(str(got["capability"])):
            gate += 1
        outcomes[outcome] += 1
        lines.append({"text": row["text"], "expected": expected, "got": got, "outcome": outcome,
                      "parser_note": note, "seconds": round(seconds, 3)})
    return {
        "lines": len(rows),
        "outcomes": dict(outcomes),
        "per_capability": {k: (right_per_capability[k], per_capability[k]) for k in sorted(per_capability)},
        "refused_by_parser": sum(1 for line in lines if line["parser_note"]
                                 and not line["parser_note"].startswith(RUNNER_FAILED)),
        "runner_failed": sum(1 for line in lines if line["parser_note"].startswith(RUNNER_FAILED)),
        "gate_failures": gate,
        "details": lines,
    }


def run(config_path: Path, set_path: Path, apps_path: Path, limit: int | None) -> tuple[dict, list[str]]:
    config = load_config(config_path)
    if config.router is None:
        raise ConfigError(f"{config_path} has no [router] table; there is no router to score")
    rows = load_rows(set_path)
    if limit is not None:
        rows = rows[:limit]
    apps = json.loads(apps_path.read_text(encoding="utf-8"))["apps"]
    answers: list[tuple[dict, str, float]] = []
    with tempfile.TemporaryDirectory(prefix="nova-n3-eval-") as scratch:
        base = Path(scratch)
        desktop = DesktopConfig(apps={name: (sys.executable,) for name in apps}, folders=(base,),
                                confirm_all_actions=True, documents_folder=base, spotify_search=True)
        bridge = DesktopBridge(desktop, NoPlatform(), memory=MemoryStore(base / "memories.jsonl"))
        actions = bridge.action_specs()
        schema = write_schema_file(base / "router", actions)
        command = tuple(str(schema) if part == SCHEMA_FILE_TOKEN else part for part in config.router.command)
        router = Router(LocalCommandBackend(replace(config.router, command=command), base), actions)
        for row in rows:
            started = time.monotonic()
            route = router.route(row["text"])
            seconds = time.monotonic() - started
            if route.kind == CHAT_ROUTE or route.capability is None or route.args is None:
                got: dict[str, object] = {"route": CHAT_ROUTE}
            else:
                got = {"route": "action", "capability": route.capability, "args": dict(route.args)}
            answers.append((got, route.note, seconds))
    return score(rows, answers), list(config.router.command)


def report(result: dict, command: list[str], set_path: Path, limit: int | None, total: int) -> str:
    out = [
        f"N3 router eval, {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"router command: {json.dumps(command)}",
        f"set: {set_path} ({result['lines']} lines"
        + (f"; PARTIAL: the first {result['lines']} of {total}" if limit is not None and result["lines"] < total else "")
        + ")",
        "",
        "right per capability:",
    ]
    for kind, (right, count) in result["per_capability"].items():
        out.append(f"  {kind:15} {right:3} of {count}")
    outcomes = result["outcomes"]
    seconds = [line["seconds"] for line in result["details"]]
    commands = sum(count for kind, (_, count) in result["per_capability"].items() if kind != "chat")
    right_commands = sum(right for kind, (right, _) in result["per_capability"].items() if kind != "chat")
    out += [
        "",
        f"commands right: {right_commands} of {commands}; chat lines right: "
        f"{result['per_capability'].get('chat', (0, 0))[0]} of {result['per_capability'].get('chat', (0, 0))[1]}",
        f"right {outcomes.get('right', 0)}, missed {outcomes.get('missed', 0)}, "
        f"wrong actions {outcomes.get('WRONG ACTION', 0)}, false actions {outcomes.get('FALSE ACTION', 0)}",
        f"outputs refused by the strict parser (answered as chat): {result['refused_by_parser']}",
        f"seconds per line: median {statistics.median(seconds):.2f}, max {max(seconds):.2f}" if seconds else "",
        f"wrong or false actions that would run without asking: {result['gate_failures']}",
        verdict(result),
        "",
    ]
    for line in result["details"]:
        if line["outcome"] != "right":
            note = f"  [{line['parser_note']}]" if line["parser_note"] else ""
            out.append(f"{line['outcome']}: {line['text']!r} -> {json.dumps(line['got'], ensure_ascii=False)}{note}")
    return "\n".join(out)


def verdict(result: dict) -> str:
    if result["runner_failed"]:
        return (f"RUN INVALID: the runner failed on {result['runner_failed']} of {result['lines']} lines "
                f"(see the notes below); this measures nothing")
    if result["gate_failures"]:
        return "GATE NOT MET (docs/work-orders.md N3 needs none)"
    return "GATE MET (none); compare the right counts before choosing"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N3: score the configured router on a hand-written command set")
    parser.add_argument("--config", required=True, type=Path, help="the NovaCore configuration whose [router] is scored")
    parser.add_argument("--set", type=Path, default=HERE / "commands.jsonl")
    parser.add_argument("--apps", type=Path, default=HERE / "apps.json")
    parser.add_argument("--transcript", type=Path, help="also write every line's answer as JSON lines (never overwrites)")
    parser.add_argument("--limit", type=int, help="score only the first N lines (reported as PARTIAL)")
    arguments = parser.parse_args(argv)
    if arguments.transcript is not None and arguments.transcript.exists():
        print(f"run_eval: {arguments.transcript} already exists; nothing was overwritten", file=sys.stderr)
        return 2
    try:
        total = len(load_rows(arguments.set))
        result, command = run(arguments.config, arguments.set, arguments.apps, arguments.limit)
    except (ConfigError, ValueError, OSError) as error:
        print(f"run_eval: {error}", file=sys.stderr)
        return 2
    print(report(result, command, arguments.set, arguments.limit, total))
    if arguments.transcript is not None:
        with arguments.transcript.open("x", encoding="utf-8", newline="\n") as handle:
            for line in result["details"]:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    if result["runner_failed"]:
        return 2
    return 0 if result["gate_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
