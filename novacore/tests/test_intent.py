"""N4: NOVA's own command model, the stdlib-Python runtime.

Oracles:
- FNV-1a: the vectors published with the algorithm
  (http://www.isthe.com/chongo/tech/comp/fnv/), not this file's code.
- The model: intent/model/fixture.json, written by the Rust trainer
  (`intent-train fixture`), a separate implementation of intent/README.md.
  The Python runtime must reproduce its tokens and every probability.
- The gate: intent/data/test.jsonl, written by hand and committed (fb055bf)
  before any generator, training code or model existed. Its numbers come from
  docs/work-orders.md N4, fixed before training.
- Durations, spans and app names: hand-written cases below.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from novacore import intent
from novacore.config import ModelConfig
from novacore.llm import LocalCommandBackend
from novacore.router import ActionSpec, Router, write_schema_file
from novacore.capabilities import ChoiceArg, IntArg, TextArg

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "intent" / "model" / "nova-intent.bin"
FIXTURE = ROOT / "intent" / "model" / "fixture.json"
TEST_SET = ROOT / "intent" / "data" / "test.jsonl"
TEST_CONFIG = ROOT / "intent" / "data" / "test-config.json"

_MODEL: intent.Model | None = None


def model() -> intent.Model:
    global _MODEL
    if _MODEL is None:
        _MODEL = intent.Model(MODEL)
    return _MODEL


class TokenAndFeatureTests(unittest.TestCase):
    def test_fnv1a_matches_the_published_vectors(self) -> None:
        self.assertEqual(0x811C9DC5, intent.fnv1a32(b""))
        self.assertEqual(0xE40C292C, intent.fnv1a32(b"a"))
        self.assertEqual(0xBF9CF968, intent.fnv1a32(b"foobar"))

    def test_tokens_keep_their_place_in_the_text(self) -> None:
        text = "Mom's café, INVOICE-42"
        words = intent.tokens(text)
        self.assertEqual(["mom", "s", "caf", "invoice", "42"], [w.text for w in words])
        self.assertEqual(["Mom", "s", "caf", "INVOICE", "42"], [text[w.start:w.end] for w in words])

    def test_features_are_the_word_then_its_trigrams(self) -> None:
        self.assertEqual(["w:ab", "t:<ab", "t:ab>"], intent.token_features("ab"))
        self.assertEqual(["w:a", "t:<a>"], intent.token_features("a"))


class DecodingTests(unittest.TestCase):
    def test_durations(self) -> None:
        cases = {
            "10 minutes": 10, "ten minutes": 10, "twenty five minutes": 25, "an hour": 60, "a minute": 1,
            "half an hour": 30, "2 hours": 120, "1 hr": 60, "3 min": 3, "90": 90, "five": 5,
            "an hour and a half": 90, "a quarter of an hour": 15, "forty-five mins": 45,
            "0 minutes": None, "1441 minutes": None, "25 hours": None, "banana": None,
            "10 seconds": None, "": None, "5 minutes 3": None,
        }
        for spoken, minutes in cases.items():
            with self.subTest(spoken):
                self.assertEqual(minutes, intent.parse_minutes(spoken))

    def test_first_span_of_each_slot(self) -> None:
        words = intent.tokens("remind me in 10 minutes to check the oven")
        tags = [intent.TAGS.index(t) for t in ("O", "O", "O", "B-num", "I-num", "O", "B-label", "I-label", "I-label")]
        self.assertEqual({"num": (3, 4), "label": (6, 8)}, intent.first_spans(words, tags))
        # An I- tag with no B- before it starts a span; a second span is ignored.
        tags = [intent.TAGS.index(t) for t in ("I-query", "I-query", "O", "B-query", "O", "O", "O", "O", "O")]
        self.assertEqual({"query": (0, 1)}, intent.first_spans(words, tags))

    def test_app_names(self) -> None:
        offered = ("vscode", "calculator", "spotify")
        self.assertEqual("vscode", intent.match_app(["vs", "code"], offered))
        self.assertEqual("calculator", intent.match_app(["calculator", "app"], offered))
        self.assertIsNone(intent.match_app(["photoshop"], offered))
        self.assertIsNone(intent.match_app([], offered))
        self.assertIsNone(intent.match_app(["code"], ("code", "Code")))  # two offered names match: refuse

    def test_normalising_for_the_gate(self) -> None:
        self.assertEqual("eggs", intent.normalise("the eggs"))
        self.assertEqual("exam is on monday", intent.normalise("My exam is on Monday."))
        self.assertEqual("call mom", intent.normalise("to call mom"))


class ModelFileTests(unittest.TestCase):
    def test_a_damaged_file_is_refused(self) -> None:
        data = MODEL.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            for name, broken in (("magic", b"NOVAINT2" + data[8:]), ("short", data[:-4]), ("long", data + b"\0\0\0\0")):
                with self.subTest(name):
                    path = Path(directory) / name
                    path.write_bytes(broken)
                    with self.assertRaises(intent.ModelError):
                        intent.Model(path)


class AgreesWithTheRustTrainerTests(unittest.TestCase):
    def test_tokens_and_every_probability_match_the_trainer(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        self.assertEqual(12, len(cases))
        worst = 0.0
        for case in cases:
            with self.subTest(case["text"]):
                self.assertEqual(case["tokens"], [w.text for w in intent.tokens(case["text"])])
                reading = intent.read(model(), case["text"])
                assert reading is not None
                self.assertEqual(len(case["tags"]), len(reading.tag_probs))
                pairs = list(zip(case["intent"], reading.intent_probs))
                for rust_row, python_row in zip(case["tags"], reading.tag_probs):
                    pairs += list(zip(rust_row, python_row))
                for rust, python in pairs:
                    worst = max(worst, abs(rust - python))
                    self.assertAlmostEqual(rust, python, delta=1e-9)
        print(f"\n[intent] largest Rust/Python probability difference: {worst:.3e}", file=sys.stderr)


class OfferTests(unittest.TestCase):
    def test_an_action_the_schema_does_not_offer_is_chat(self) -> None:
        everything = intent.Offer(intent.ALL_CAPABILITIES, ("spotify",))
        self.assertEqual({"route": "action", "capability": "apps.open", "args": {"app": "spotify"}},
                         intent.decide(model(), "open spotify", everything))
        without_apps = intent.Offer(intent.ALL_CAPABILITIES - {"apps.open"}, ())
        self.assertEqual({"route": "chat"}, intent.decide(model(), "open spotify", without_apps))
        other_apps = intent.Offer(intent.ALL_CAPABILITIES, ("vlc",))
        self.assertEqual({"route": "chat"}, intent.decide(model(), "open spotify", other_apps))

    def test_a_capability_the_schema_does_not_offer_is_chat_even_without_arguments(self) -> None:
        # Added after plant P5 (runtime ignores the offer) passed the test
        # above: apps.open was refused there by the app list, not by the
        # capability check. time.now has no arguments, so only the capability
        # check can refuse it.
        offered = intent.Offer(intent.ALL_CAPABILITIES, ())
        self.assertEqual({"route": "action", "capability": "time.now", "args": {}},
                         intent.decide(model(), "what time is it", offered))
        not_offered = intent.Offer(intent.ALL_CAPABILITIES - {"time.now"}, ())
        self.assertEqual({"route": "chat"}, intent.decide(model(), "what time is it", not_offered))

    def test_a_line_too_long_to_read_is_chat(self) -> None:
        offer = intent.Offer(intent.ALL_CAPABILITIES, ("spotify",))
        self.assertEqual({"route": "chat"}, intent.decide(model(), "open spotify " + "now " * 70, offer))


class RouterRunnerTests(unittest.TestCase):
    """The runner as N1 runs it: a subprocess reading the router prompt on stdin."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.actions = [
            ActionSpec("apps.open", "Open an application.", {"app": ChoiceArg(("spotify", "notepad"))}, "open spotify", {"app": "spotify"}),
            ActionSpec("timer.set", "Set a timer.", {"minutes": IntArg(1, 1440), "label": TextArg(0, 40)},
                       "remind me in 10 minutes to stretch", {"minutes": 10, "label": "stretch"}),
            ActionSpec("time.now", "Tell the time.", {}, "what time is it", {}),
        ]
        self.schema = write_schema_file(self.base / "router", self.actions)

    def _router(self, model_path: Path) -> Router:
        # Exactly as config.example.toml runs it: the module as a script, in
        # isolated mode (-I), under llm.py's scrubbed environment (no PYTHONPATH).
        script = ROOT / "novacore" / "src" / "novacore" / "intent.py"
        command = (sys.executable, "-I", str(script), "--model", str(model_path), str(self.schema))
        config = ModelConfig(command=command, timeout_seconds=60, max_output_chars=2000, use_stdin=True)
        return Router(LocalCommandBackend(config, self.base), self.actions)

    def test_the_runner_answers_through_the_n1_parser(self) -> None:
        router = self._router(MODEL)
        route = router.route("remind me in 10 minutes to stretch")
        self.assertEqual(("action", "timer.set", {"minutes": 10, "label": "stretch"}), (route.kind, route.capability, route.args))
        self.assertEqual(("chat", ""), (router.route("what is the volume of a sphere").kind, ""))

    def test_any_fault_is_chat(self) -> None:
        route = self._router(self.base / "no-such-model.bin").route("open spotify")
        self.assertEqual(("chat", ""), (route.kind, route.note))
        self.assertEqual('{"route": "chat"}', intent.run_router(MODEL, self.schema, "no owner line here"))
        self.assertEqual('{"route": "chat"}', intent.run_router(MODEL, self.base / "missing.json", "Owner: open spotify"))


class GateTests(unittest.TestCase):
    """docs/work-orders.md N4: 0 actions on chat lines, at most 2 wrong
    actions, at least 108 of 127 action lines exactly right."""

    def test_the_hand_written_test_set_meets_the_gate(self) -> None:
        rows = [json.loads(line) for line in TEST_SET.read_text(encoding="utf-8").splitlines() if line.strip()]
        apps = json.loads(TEST_CONFIG.read_text(encoding="utf-8"))["apps"]
        result = intent.evaluate(model(), rows, apps)
        failures = result.pop("failures")
        report = json.dumps(result) + "\n" + "\n".join(f"{k}: {t!r} -> {g}" for k, t, g in failures)
        self.assertEqual((227, 127, 100), (result["lines"], result["action_lines"], result["chat_lines"]))
        self.assertEqual(0, result["false_actions"], report)
        self.assertLessEqual(result["wrong_actions"], 2, report)
        self.assertGreaterEqual(result["right_actions"], 108, report)


if __name__ == "__main__":
    unittest.main()
