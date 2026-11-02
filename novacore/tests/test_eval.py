"""N3: the router eval (novacore/eval/run_eval.py) and its command set.

Oracles:
- A small set run through the STUB router runner (scripted keyword answers,
  NOT a model), with every count worked out by hand below from what the STUB
  answers (novacore/tests/STUB_router_runner.py) and the gate rule in
  docs/work-orders.md N3.
- NOVA's own model through the eval (a subprocess per line and N1's strict
  parser) against the in-process scorer from N4 (intent.evaluate), which
  shares neither the subprocess nor the parser.
- The command set's counts against the numbers its README states, and its
  expected actions against the broker's own argument rules.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from novacore import intent
from novacore.capabilities import argument_problem
from novacore.config import DesktopConfig
from novacore.desktop import DesktopBridge
from novacore.memory import MemoryStore
from novacore.router import router_prompt

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
EVAL = ROOT / "novacore" / "eval"

spec = importlib.util.spec_from_file_location("run_eval", EVAL / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)  # type: ignore[union-attr]


def _config(folder: Path, router: list[str]) -> Path:
    path = folder / "config.toml"
    path.write_text(
        f"[model]\ncommand = {json.dumps([sys.executable, str(HERE / 'fake_runner.py')])}\n"
        "timeout_seconds = 10\nmax_output_chars = 1000\n\n"
        f"[router]\ncommand = {json.dumps(router)}\nuse_stdin = true\ntimeout_seconds = 60\nmax_output_chars = 2000\n\n"
        "[conversation]\nhistory_turns = 2\nmemory_results = 2\n\n[storage]\nstate_dir = \"state\"\n",
        encoding="utf-8",
    )
    return path


def _main(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with patch("sys.stdout", output), patch("sys.stderr", output):
        status = run_eval.main(argv)
    return status, output.getvalue()


def _action(words: str, capability: str, **args: object) -> dict:
    return {"text": words, "route": "action", "capability": capability, "args": args}


def _chat(words: str) -> dict:
    return {"text": words, "route": "chat"}


class HandCountedTests(unittest.TestCase):
    # What the STUB answers, and so the outcome, worked out by hand per line.
    ROWS = [
        _action("what time is it now", "time.now"),                         # time.now: right
        _action("find chapter notes", "files.find", query="chapter notes"),  # files.find "chapter notes": right
        _action("find my essay drafts", "files.find", query="essay drafts"),  # "my essay drafts": right ("my" dropped)
        _action("start notepad", "apps.open", app="notepad"),                # apps.open notepad: right
        _chat("hello there"),                                                 # chat: right
        _action("start the music app", "apps.open", app="spotify"),          # app "the music app" refused by the parser: missed
        _chat("find a way to relax"),                                         # files.find: FALSE, runs at once: gate
        _chat("what time zone is japan in"),                                  # time.now: FALSE, runs at once: gate
        _chat("remember when we went to the beach"),                          # memory.save: FALSE, asks first: no gate
        _action("find my old essay", "files.open", query="old essay"),       # files.find: WRONG, runs at once: gate
        _action("remember this as a pdf called notes", "docs.create",        # memory.save: WRONG, asks first: no gate
                title="notes", format="pdf", text=""),
    ]

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.set = self.base / "set.jsonl"
        self.set.write_text("".join(json.dumps(row) + "\n" for row in self.ROWS), encoding="utf-8")
        self.apps = self.base / "apps.json"
        self.apps.write_text(json.dumps({"apps": ["notepad", "spotify"]}), encoding="utf-8")
        self.config = _config(self.base, [sys.executable, str(HERE / "STUB_router_runner.py"), "{schema_file}"])

    def test_counts_and_gate(self) -> None:
        result, _ = run_eval.run(self.config, self.set, self.apps, None)
        self.assertEqual({"right": 5, "missed": 1, "FALSE ACTION": 3, "WRONG ACTION": 2}, result["outcomes"])
        self.assertEqual(3, result["gate_failures"])
        self.assertEqual(1, result["refused_by_parser"])
        self.assertEqual(0, result["runner_failed"])
        self.assertEqual({"apps.open": (1, 2), "chat": (1, 4), "docs.create": (0, 1), "files.find": (2, 2),
                          "files.open": (0, 1), "time.now": (1, 1)}, result["per_capability"])

    def test_report_exit_status_partial_runs_and_transcripts(self) -> None:
        transcript = self.base / "t.jsonl"
        status, text = _main(["--config", str(self.config), "--set", str(self.set), "--apps", str(self.apps),
                              "--transcript", str(transcript)])
        self.assertEqual(1, status)
        self.assertIn("right 5, missed 1, wrong actions 2, false actions 3", text)
        self.assertIn("GATE NOT MET", text)
        self.assertEqual(11, len(transcript.read_text(encoding="utf-8").splitlines()))
        status, text = _main(["--config", str(self.config), "--set", str(self.set), "--apps", str(self.apps),
                              "--transcript", str(transcript)])
        self.assertEqual((2, True), (status, "already exists" in text))
        status, text = _main(["--config", str(self.config), "--set", str(self.set), "--apps", str(self.apps),
                              "--limit", "5"])
        self.assertEqual(0, status)  # the first five lines are all right
        self.assertIn("(5 lines; PARTIAL: the first 5 of 11)", text)

    def test_a_runner_that_fails_makes_the_run_invalid_never_a_pass(self) -> None:
        config = _config(self.base, [str(self.base / "no-such-runner"), "{schema_file}"])
        status, text = _main(["--config", str(config), "--set", str(self.set), "--apps", str(self.apps)])
        self.assertEqual(2, status)
        self.assertIn("RUN INVALID: the runner failed on 11 of 11 lines", text)
        self.assertNotIn("GATE MET", text)


class CrossCheckTests(unittest.TestCase):
    def test_nova_model_through_the_eval_agrees_with_the_in_process_scorer(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        router = [sys.executable, "-I", str(ROOT / "novacore" / "src" / "novacore" / "intent.py"),
                  "--model", str(ROOT / "intent" / "model" / "nova-intent.bin"), "{schema_file}"]
        result, _ = run_eval.run(_config(Path(directory.name), router), EVAL / "commands.jsonl", EVAL / "apps.json", None)
        rows = run_eval.load_rows(EVAL / "commands.jsonl")
        apps = json.loads((EVAL / "apps.json").read_text(encoding="utf-8"))["apps"]
        direct = intent.evaluate(intent.Model(ROOT / "intent" / "model" / "nova-intent.bin"), rows, apps)
        outcomes = result["outcomes"]
        self.assertEqual(
            (direct["right_actions"] + direct["chat_lines"] - direct["false_actions"], direct["missed_actions"],
             direct["wrong_actions"], direct["false_actions"]),
            (outcomes.get("right", 0), outcomes.get("missed", 0), outcomes.get("WRONG ACTION", 0),
             outcomes.get("FALSE ACTION", 0)),
        )
        self.assertEqual(0, result["runner_failed"])


class CommandSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = run_eval.load_rows(EVAL / "commands.jsonl")
        self.apps = json.loads((EVAL / "apps.json").read_text(encoding="utf-8"))["apps"]

    def test_counts_match_the_readme(self) -> None:
        stated = {"apps.open": 12, "files.find": 6, "files.open": 5, "media.control": 6, "volume.control": 6,
                  "system.lock": 3, "time.now": 3, "timer.set": 6, "memory.save": 4, "music.play": 7,
                  "docs.create": 6, "chat": 56}  # novacore/eval/README.md
        counted: dict[str, int] = {}
        for row in self.rows:
            counted[row.get("capability", "chat")] = counted.get(row.get("capability", "chat"), 0) + 1
        self.assertEqual(stated, counted)
        self.assertEqual(120, len(self.rows))

    def test_every_expected_action_passes_the_brokers_rules_and_no_line_is_a_prompt_example(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch)
            bridge = DesktopBridge(
                DesktopConfig(apps={a: (sys.executable,) for a in self.apps}, folders=(base,), confirm_all_actions=True,
                              documents_folder=base, spotify_search=True),
                run_eval.NoPlatform(), memory=MemoryStore(base / "m.jsonl"),
            )
            for row in self.rows:
                if row["route"] == "action":
                    with self.subTest(row["text"]):
                        self.assertIsNone(argument_problem(bridge.arg_specs(row["capability"]), row["args"]))
            prompt = router_prompt("-", bridge.action_specs())
        examples = {line[len("Owner: "):].casefold() for line in prompt.splitlines() if line.startswith("Owner: ")}
        self.assertEqual(set(), {row["text"].casefold() for row in self.rows} & examples)

    def test_no_line_repeats_an_earlier_set_or_the_generated_data(self) -> None:
        earlier: set[str] = set()
        for name in ("test.jsonl", "test2.jsonl", "test3.jsonl", "dev-hand.jsonl"):
            for line in (ROOT / "intent" / "data" / name).read_text(encoding="utf-8").splitlines():
                earlier.add(json.loads(line)["text"].casefold().strip())
        for name in ("train.tsv", "dev.tsv"):
            for line in (ROOT / "intent" / "data" / name).read_text(encoding="utf-8").splitlines():
                earlier.add(line.split("\t")[1].casefold().strip())
        self.assertEqual(set(), {row["text"].casefold().strip() for row in self.rows} & earlier)


if __name__ == "__main__":
    unittest.main()
