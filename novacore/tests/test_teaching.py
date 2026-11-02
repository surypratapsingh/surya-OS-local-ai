"""N4d: NOVA learns from the owner's own commands.

Oracles:
- Lesson keys: hand-written pairs.
- The lesson log: read back with json and hashlib, the chain re-derived from
  novacore/SCOPE.md "Log integrity" (test_desktop._chain), not from
  chainlog.py; tampering is done by editing the file's bytes.
- What happened: the recording fake platform, the outbox read and verified
  independently (test_desktop._events), and how many prompts the router
  stand-in received; never the assistant's own account of it.
- Training lines: written out by hand per case, then read back through the
  command model's runtime decoder (intent.answer, given one-hot outputs), which
  must give the lesson's own action.
- CLI and voice runs: the real cli.main with the STUB router runner (scripted,
  not a model) or NOVA's own command model, and a canary line said without the
  wake word, searched for afterwards in every file the run could write.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from novacore import cli, intent
from novacore.assistant import LEARNING_OFF, Assistant
from novacore.capabilities import CapabilityBroker
from novacore.companion import Companion
from novacore.config import ConfigError, ConversationConfig, DesktopConfig, load_config
from novacore.desktop import DesktopBridge
from novacore.memory import MemoryStore
from novacore.router import Router
from novacore.teaching import TeachingStore, key, training_line
from test_assistant import CHAT, Scripted, _action
from test_desktop import FakePlatform, _chain, _events

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class KeyTests(unittest.TestCase):
    def test_wake_and_politeness_words_are_dropped_only_at_the_ends(self) -> None:
        cases = {
            "Nova, could you open Spotify please?": "open spotify",
            "open spotify": "open spotify",
            "OK Nova. Open   SPOTIFY!!": "open spotify",
            "hey nova can you please lock the screen thanks": "lock the screen",
            "open spotify thank you nova": "open spotify",
            "open the spotify": "open the spotify",
            "play i love you": "play i love you",
            "play see you again please": "play see you again",
            "don't open spotify": "don t open spotify",
            "Écoute la musique": "écoute la musique",
            "ＯＰＥＮ ｓｐｏｔｉｆｙ": "open spotify",  # full-width letters (NFKC)
            "गाना चलाओ": "गाना चलाओ",  # vowel signs (category Mc) stay in their words
            "thank you": "",
            "please": "",
            "nova": "",
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(expected, key(text))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "lessons.jsonl"
        self.anchor = self.path.with_name("lessons.jsonl.anchor")
        self.store = TeachingStore(self.path)

    def _forge(self, extra: dict) -> None:
        """Append a record whose chain and anchor are right, as anyone who can
        write the folder could (SCOPE.md says the chain has no key)."""
        records = _records(self.path)
        fields = {k: v for k, v in records[-1].items() if k != "chain_hash"}
        fields.update(extra)
        chained = {**fields, "chain_hash": _chain(fields, records[-1]["chain_hash"])}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(chained, sort_keys=True) + "\n")
        self.anchor.write_text(json.dumps({"count": len(records) + 1, "head": chained["chain_hash"]}), encoding="utf-8")

    def test_lessons_are_chained_and_anchored_as_scope_md_states(self) -> None:
        self.store.add("open spotify", "do", "apps.open", {"app": "spotify"}, source="confirmed")
        self.store.add("how are you", "chat", source="taught")
        previous = "0" * 64
        for record in _records(self.path):
            fields = {k: v for k, v in record.items() if k != "chain_hash"}
            self.assertEqual(_chain(fields, previous), record["chain_hash"])
            previous = record["chain_hash"]
        self.assertEqual({"count": 2, "head": previous}, json.loads(self.anchor.read_text(encoding="utf-8")))

    def test_a_changed_lesson_is_refused_and_never_extended(self) -> None:
        self.store.add("open spotify", "do", "apps.open", {"app": "spotify"}, source="confirmed")
        self.path.write_bytes(self.path.read_bytes().replace(b'"spotify"}', b'"notepad"}'))
        changed = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "lesson chain hash mismatch at .*:1"):
            self.store.lookup("open spotify")
        with self.assertRaisesRegex(ValueError, "lesson chain hash mismatch"):
            self.store.add("lock it", "do", "system.lock", {}, source="taught")
        self.assertEqual(changed, self.path.read_bytes())

    def test_a_dropped_last_lesson_or_a_missing_anchor_is_refused(self) -> None:
        self.store.add("open spotify", "do", "apps.open", {"app": "spotify"}, source="confirmed")
        self.store.add("how are you", "chat", source="taught")
        first = self.path.read_text(encoding="utf-8").splitlines()[0]
        self.path.write_text(first + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "anchor mismatch"):
            self.store.all()
        self.anchor.unlink()
        with self.assertRaisesRegex(ValueError, "no anchor"):
            self.store.all()

    def test_a_forged_record_with_a_valid_chain_still_meets_the_record_rules(self) -> None:
        self.store.add("open spotify", "do", "apps.open", {"app": "spotify"}, source="confirmed")
        self._forge({"text": "save the gate code", "capability": "memory.save", "arguments": {"text": "4321"}})
        with self.assertRaisesRegex(ValueError, "memory.save is never learned"):
            self.store.all()

    def test_a_record_with_an_unknown_field_is_refused(self) -> None:
        self.store.add("open spotify", "do", "apps.open", {"app": "spotify"}, source="confirmed")
        self._forge({"run_without_any_check": True})
        with self.assertRaisesRegex(ValueError, "invalid lesson record at .*:2"):
            self.store.all()

    def test_lookup_returns_the_latest_lesson_for_the_same_words(self) -> None:
        self.store.add("open spotify", "do", "apps.open", {"app": "spotify"}, source="confirmed")
        self.store.add("Nova, open Spotify please", "not", "apps.open", {"app": "spotify"}, source="cancelled")
        self.store.add("how are you", "chat", source="taught")
        found = self.store.lookup("hey nova open spotify")
        self.assertEqual(("not", "Nova, open Spotify please"), (found.verdict, found.text))
        self.assertIsNone(self.store.lookup("open the spotify"))
        self.assertIsNone(self.store.lookup("please"))

    def test_invalid_lessons_are_not_written(self) -> None:
        cases = [
            (("please", "chat", None, None, "taught"), "nothing to learn"),
            (("x" * 501, "chat", None, None, "taught"), "longer than 500"),
            (("open it", "do", None, None, "confirmed"), "unknown capability"),
            (("open it", "do", "apps.open", None, "confirmed"), "JSON object"),
            (("open it", "chat", None, None, "confirmed"), "cannot come from 'confirmed'"),
            (("open it", "not", "apps.open", {}, "taught"), "cannot come from 'taught'"),
            (("save it", "do", "memory.save", {"text": "x"}, "confirmed"), "never learned"),
            (("open it", "maybe", None, None, "taught"), "unknown verdict"),
            (("open it", "chat", "apps.open", {}, "taught"), "no capability"),
            (("open it", "do", "apps.open", {"app": float("nan")}, "taught"), "not plain JSON"),
        ]
        for (text, verdict, capability, arguments, source), message in cases:
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                self.store.add(text, verdict, capability, arguments, source=source)
        self.assertFalse(self.path.exists())


class LessonTestCase(unittest.TestCase):
    def build(
        self,
        router_outputs: tuple[str, ...],
        *,
        confirm_all: bool = True,
        trust: bool = True,
        learning: bool = True,
        router: bool = True,
        apps: dict | None = None,
        base: Path | None = None,
    ) -> None:
        if base is None:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            base = Path(directory.name)
        self.base = base
        (base / "docs").mkdir(exist_ok=True)
        self.events_path = base / "state" / "events.jsonl"
        self.lessons_path = base / "state" / "lessons.jsonl"
        self.memory_path = base / "state" / "memories.jsonl"
        memory = MemoryStore(self.memory_path)
        self.platform = FakePlatform()
        self.router_backend = Scripted(*router_outputs)
        companion = Companion(Scripted("chat reply"), memory, ConversationConfig(history_turns=6, memory_results=3))
        bridge = DesktopBridge(
            DesktopConfig(
                apps={"music": (sys.executable,)} if apps is None else apps,
                folders=(base / "docs",),
                confirm_all_actions=confirm_all,
            ),
            self.platform,
            memory=memory,
        )
        self.assistant = Assistant(
            companion,
            CapabilityBroker(self.events_path, bridge),
            Router(self.router_backend, bridge.action_specs()) if router else None,
            bridge,
            confirm_all_actions=confirm_all,
            teaching=TeachingStore(self.lessons_path) if learning else None,
            trust_learned=trust,
        )

    def run_line(self, line: str) -> str:
        return cli._run_line(line, self.assistant, proposals=None)  # type: ignore[arg-type]

    def lessons(self) -> list[tuple]:
        """(verdict, text, capability, source) per record, read with json alone."""
        if not self.lessons_path.exists():
            return []
        return [(r["verdict"], r["text"], r["capability"], r["source"]) for r in _records(self.lessons_path)]

    def runs(self) -> list[tuple]:
        if not self.events_path.exists():
            return []
        return [(e["capability"]["name"], e["owner_confirmed"], e["applied"]) for e in _events(self.events_path)]


class LearnFromYesAndNoTests(LessonTestCase):
    def test_a_confirmed_action_runs_at_once_next_time_without_the_router(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),))
        self.assertIn("Type yes", self.assistant.handle("open music"))
        self.assertEqual('Started "music".\n[learned: next time these words run at once]', self.assistant.handle("yes"))
        self.assertEqual('Started "music". [learned]', self.assistant.handle("Nova, could you open music please?"))
        self.assertEqual(1, len(self.router_backend.prompts))
        self.assertEqual([("launch", (sys.executable,))] * 2, self.platform.calls)
        self.assertEqual([("apps.open", True, True), ("apps.open", False, True)], self.runs())
        self.assertEqual([("do", "open music", "apps.open", "confirmed")], self.lessons())

    def test_with_trust_learned_off_a_lesson_asks_first_and_is_not_learned_twice(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),), trust=False)
        self.assistant.handle("open music")
        self.assertIn("[learned: next time these words skip the router and ask you first]", self.assistant.handle("yes"))
        self.assertEqual(
            'NOVA would open the application "music" (apps.open). Type yes to do it, or no to cancel. [learned]',
            self.assistant.handle("open music"),
        )
        self.assertEqual('Started "music".', self.assistant.handle("yes"))
        self.assertEqual(1, len(self.router_backend.prompts))
        self.assertEqual(2, len(self.platform.calls))
        self.assertEqual(1, len(self.lessons()))

    def test_with_confirm_all_off_nothing_asks_so_nothing_is_learned_by_itself(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),), confirm_all=False)
        self.assertEqual('Started "music".', self.assistant.handle("open music"))
        self.assertEqual([], self.lessons())

    def test_a_refused_capability_is_not_offered_again_for_those_words(self) -> None:
        self.build((_action("apps.open", {"app": "music"}), _action("apps.open", {"app": "music"}), _action("time.now", {})))
        self.assistant.handle("open music")
        self.assertEqual(
            "Cancelled. Nothing was done.\n[learned: NOVA will not offer apps.open for these words again]",
            self.assistant.handle("no"),
        )
        self.assertEqual(
            "chat reply\n[learned: you said no to apps.open for these words; answered as chat]",
            self.assistant.handle("open music"),
        )
        self.assertIsNone(self.assistant.pending)
        # Another capability for the same words is still offered, and still asks.
        self.assertIn("(time.now). Type yes", self.assistant.handle("open music"))
        self.assertEqual(3, len(self.router_backend.prompts))
        self.assertEqual([], self.platform.calls)
        self.assertEqual([], self.runs())
        self.assertEqual([("not", "open music", "apps.open", "cancelled")], self.lessons())

    def test_only_the_owners_yes_or_no_makes_a_lesson(self) -> None:
        self.build((_action("apps.open", {"app": "music"}), CHAT))
        self.assistant.handle("open music")
        self.assistant.handle("tell me a joke")  # cancels by another line: not a "no"
        self.assertEqual([], self.lessons())

    def test_a_failed_run_is_not_learned(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),))
        self.assistant.handle("open music")
        self.platform.fail = "no display"
        self.assertNotIn("[learned", self.assistant.handle("yes"))
        self.assertEqual([("apps.open", True, False)], self.runs())
        self.assertEqual([], self.lessons())

    def test_memory_save_is_never_learned_and_always_asks(self) -> None:
        self.build((_action("memory.save", {"text": "gate code 4321"}),))
        self.assistant.handle("remember gate code 4321")
        saved = self.assistant.handle("yes")
        self.assertTrue(saved.startswith("Saved note "), saved)
        self.assertNotIn("[learn", saved)
        self.assistant.handle("remember gate code 4321")
        self.assistant.handle("no")
        self.assertIn("Type yes", self.assistant.handle("remember gate code 4321"))
        self.assertEqual([], self.lessons())
        self.assertFalse(self.lessons_path.exists())


class TeachTests(LessonTestCase):
    def test_teach_chat_answers_those_words_as_chat_without_the_router(self) -> None:
        # "lock in your answer" is one of the v3 model's false actions on test3.
        self.build((_action("system.lock", {}),))
        self.assertEqual(
            'Learned: "lock in your answer" is conversation; NOVA will answer it as chat.',
            self.run_line("/teach lock in your answer => chat"),
        )
        self.assertEqual("chat reply", self.assistant.handle("lock in your answer"))
        self.assertEqual([], self.router_backend.prompts)
        self.assertEqual([], self.platform.calls)

    def test_a_taught_action_works_with_no_router_at_all(self) -> None:
        self.build((CHAT,), router=False)
        self.assertEqual(
            'Learned: "music time" => apps.open {"app": "music"}. Next time NOVA does it at once.',
            self.run_line('/teach music time => apps.open {"app": "music"}'),
        )
        self.assertEqual('Started "music". [learned]', self.assistant.handle("music time please"))
        self.assertEqual([("launch", (sys.executable,))], self.platform.calls)
        self.assertEqual([("apps.open", False, True)], self.runs())

    def test_teach_refuses_what_cannot_run_here(self) -> None:
        self.build((CHAT,))
        refused = {
            "/teach open it": "usage",
            "/teach => chat": "usage",
            '/teach open it => apps.open {"app": "rm"}': "cannot teach apps.open",
            "/teach reboot => system.reboot {}": "cannot teach system.reboot: system.reboot does not run here",
            '/teach save it => memory.save {"text": "x"}': "never learned",
            "/teach open it => no.such {}": "unknown capability",
            "/teach please => system.lock": "nothing to learn",
            '/teach open it => apps.open ["music"]': "JSON object",
        }
        for line, message in refused.items():
            with self.subTest(line), self.assertRaisesRegex(ValueError, message):
                self.run_line(line)
        self.assertFalse(self.lessons_path.exists())

    def test_a_lesson_that_no_longer_fits_is_not_used(self) -> None:
        self.build((CHAT,))
        self.run_line('/teach music time => apps.open {"app": "music"}')
        # The owner then removes "music" from [desktop.apps]; the same lessons are read.
        self.build((CHAT,), apps={"notepad": (sys.executable,)}, base=self.base)
        reply = self.assistant.handle("music time")
        self.assertTrue(reply.startswith("chat reply\n[learned: the lesson for these words no longer fits"), reply)
        self.assertEqual([], self.platform.calls)
        self.assertEqual(1, len(self.router_backend.prompts))  # the router was asked instead

    def test_lessons_lists_every_lesson_and_marks_replaced_ones(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),))
        self.assistant.handle("open music")
        self.assistant.handle("no")
        self.run_line('/teach open music => apps.open {"app": "music"}')
        self.run_line("/teach good night => chat")
        lines = self.run_line("/lessons").splitlines()
        self.assertEqual(4, len(lines))
        self.assertEqual("3 lessons; for the same words, the latest counts:", lines[0])
        date = r"\d{4}-\d\d-\d\d"
        self.assertRegex(lines[1], rf'^1\. {date} cancelled: "open music" => not apps\.open \(replaced by a later lesson\)$')
        self.assertRegex(lines[2], rf'^2\. {date} taught: "open music" => apps\.open \{{"app": "music"\}}$')
        self.assertRegex(lines[3], rf'^3\. {date} taught: "good night" => chat$')
        self.assertEqual('Started "music". [learned]', self.assistant.handle("open music"))


class LearningOffAndTamperTests(LessonTestCase):
    def test_nothing_is_written_with_learning_off(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),), learning=False)
        self.assistant.handle("open music")
        self.assistant.handle("yes")
        self.assistant.handle("open music")
        self.assistant.handle("no")
        self.assertEqual(LEARNING_OFF, self.run_line("/teach good night => chat"))
        self.assertIn("Learning is off", self.run_line("/lessons"))
        self.assertEqual(2, len(self.router_backend.prompts))  # each "open music" went to the router
        self.assertEqual([], list((self.base / "state").glob("lessons*")))

    def test_a_tampered_lesson_log_stops_the_line_before_anything_runs(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),))
        self.assistant.handle("open music")
        self.assistant.handle("yes")
        self.lessons_path.write_bytes(self.lessons_path.read_bytes().replace(b"open music", b"open musik"))
        outbox = self.events_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "lesson chain hash mismatch"):
            self.assistant.handle("open music")
        self.assertEqual(1, len(self.platform.calls))
        self.assertEqual(outbox, self.events_path.read_bytes())
        self.assertEqual(1, len(self.router_backend.prompts))

    def test_a_log_broken_while_an_action_waits_is_reported_after_the_run(self) -> None:
        self.build((_action("apps.open", {"app": "music"}),))
        self.run_line("/teach good night => chat")
        self.assistant.handle("open music")
        self.lessons_path.with_name("lessons.jsonl.anchor").unlink()
        reply = self.assistant.handle("yes")
        self.assertTrue(reply.startswith('Started "music".\n[learning: '), reply)
        self.assertIn("no anchor", reply)
        self.assertEqual(1, len(self.platform.calls))
        self.assertEqual(1, len(self.lessons()))


class ExportTests(unittest.TestCase):
    # (words, verdict, capability, arguments, the training line written by hand, or None)
    CASES = [
        ("Nova open the music app please", "do", "apps.open", {"app": "music"},
         "apps.open\tNova open the music app please\tO O O B-app O O"),
        ("open media player", "do", "apps.open", {"app": "media-player"},
         "apps.open\topen media player\tO B-app I-app"),
        ("play blue in green", "do", "music.play", {"query": "blue in green"},
         "music.play\tplay blue in green\tO B-query I-query I-query"),
        ("find my class 12 physics notes", "do", "files.find", {"query": "class 12 physics notes"},
         "files.find\tfind my class 12 physics notes\tO O B-query I-query I-query I-query"),
        ("set a timer for 10 minutes for tea", "do", "timer.set", {"minutes": 10, "label": "tea"},
         "timer.set\tset a timer for 10 minutes for tea\tO O O O B-num I-num O B-label"),
        ("remind me in half an hour", "do", "timer.set", {"minutes": 30, "label": ""},
         "timer.set\tremind me in half an hour\tO O O B-num I-num I-num"),
        ("set a timer for a minute", "do", "timer.set", {"minutes": 1, "label": ""},
         "timer.set\tset a timer for a minute\tO O O O B-num I-num"),
        ("start a new text file called ideas for the blog", "do", "docs.create",
         {"title": "ideas for the blog", "format": "txt", "text": ""},
         "docs.text\tstart a new text file called ideas for the blog\tO O O O O O B-title I-title I-title I-title"),
        ("make a word file called plan saying buy milk", "do", "docs.create",
         {"title": "plan", "format": "docx", "text": "buy milk"},
         "docs.word\tmake a word file called plan saying buy milk\tO O O O O B-title O B-text I-text"),
        ("make a pdf", "do", "docs.create", {"title": "Untitled", "format": "pdf", "text": ""},
         "docs.pdf\tmake a pdf\tO O O"),
        ("skip this song", "do", "media.control", {"action": "next"}, "media.next\tskip this song\tO O O"),
        ("mute", "do", "volume.control", {"action": "mute"}, "volume.mute\tmute\tO"),
        ("lock in your answer", "chat", None, None, "chat\tlock in your answer\tO O O O"),
        ("open music", "not", "apps.open", {"app": "music"}, None),
        ("play something relaxing", "do", "music.play", {"query": "jazz"}, None),
        ("गाना चलाओ", "chat", None, None, None),
    ]

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TeachingStore(Path(directory.name) / "lessons.jsonl")
        for text, verdict, capability, arguments, _ in self.CASES:
            source = {"do": "taught", "chat": "taught", "not": "cancelled"}[verdict]
            self.store.add(text, verdict, capability, arguments, source=source)

    def test_each_lesson_gives_the_hand_written_line_or_a_reason(self) -> None:
        for lesson, (text, _, _, _, expected) in zip(self.store.all(), self.CASES, strict=True):
            with self.subTest(text):
                line, reason = training_line(lesson)
                self.assertEqual(expected, line)
                self.assertEqual(expected is None, bool(reason))

    def test_the_runtime_decoder_reads_each_line_back_as_the_lessons_action(self) -> None:
        offer = intent.Offer(intent.ALL_CAPABILITIES, ("music", "media-player"))
        checked = 0
        for lesson in self.store.all():
            line, _ = training_line(lesson)
            if line is None or lesson.verdict != "do":
                continue
            name, text, tags = line.split("\t")
            one_hot = [1.0 if i == intent.INTENTS.index(name) else 0.0 for i in range(len(intent.INTENTS))]
            rows = [[1.0 if t == intent.TAGS.index(tag) else 0.0 for t in range(len(intent.TAGS))] for tag in tags.split()]
            answer = intent.answer(intent.Reading(one_hot, rows), text, offer)
            with self.subTest(text):
                self.assertEqual({"route": "action", "capability": lesson.capability, "args": lesson.arguments}, answer)
            checked += 1
        self.assertEqual(12, checked)


@unittest.skipUnless(sys.platform.startswith("linux"), "the CLI run starts a real program; checked on Linux")
class CliLessonTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        docs = self.base / "docs"
        docs.mkdir()
        (docs / "chapter one.pdf").write_text("x", encoding="utf-8")
        self.marker = self.base / "probe-runs"
        probe = [sys.executable, "-c", f"open({str(self.marker)!r}, 'a').write('ran\\n')"]
        self.config = self.base / "config.toml"
        self.text = (
            "[model]\n"
            f"command = {json.dumps([sys.executable, str(HERE / 'fake_runner.py')])}\n"
            "timeout_seconds = 10\nmax_output_chars = 1000\nuse_stdin = true\n\n"
            "[router]\n"
            f"command = {json.dumps([sys.executable, str(HERE / 'STUB_router_runner.py'), '{schema_file}'])}\n"
            "timeout_seconds = 10\nmax_output_chars = 2000\nuse_stdin = true\n\n"
            "[desktop]\nconfirm_all_actions = true\n"
            f"folders = {json.dumps([str(docs)])}\n\n"
            f"[desktop.apps]\nprobe = {json.dumps(probe)}\n\n"
            "[conversation]\nhistory_turns = 2\nmemory_results = 2\n\n"
            "[storage]\nstate_dir = \"state\"\n\n"
            "[learning]\nenabled = true\n"
        )
        self.config.write_text(self.text, encoding="utf-8")
        self.state = self.base / "state"

    def _run(self, argv: list[str], inputs: list[str] | None = None, status: int = 0) -> str:
        output = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", output), \
                patch("builtins.input", side_effect=inputs or []):
            self.assertEqual(status, cli.main(argv))
        return output.getvalue()

    def _session(self, runs: int) -> str:
        transcript = self._run(["--config", str(self.config)], [
            "start probe", "yes",                   # confirmed: a "do" lesson
            "nova, start probe please",             # the STUB router would say chat to these words
            "find chapter", "no",                   # refused: a "not" lesson
            "find chapter",
            "/teach good morning => chat", "good morning",
            '/teach play my song => apps.open {"app": "probe"}', "play my song",
            "/lessons", "/quit",
        ])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (not self.marker.exists()
                                               or len(self.marker.read_text().splitlines()) < runs):
            time.sleep(0.05)
        from novacore import desktop  # let the launched probes finish before the folder goes
        for child in list(desktop._LAUNCHED):
            child.wait(timeout=10)
        return transcript

    def test_interactive_session_learns_and_exports(self) -> None:
        transcript = self._session(3)
        self.assertIn('NOVA would open the application "probe" (apps.open). Type yes', transcript)
        self.assertIn('Started "probe".\n[learned: next time these words run at once]', transcript)
        self.assertEqual(2, transcript.count('Started "probe". [learned]'))
        self.assertIn("Cancelled. Nothing was done.\n[learned: NOVA will not offer files.find for these words again]",
                      transcript)
        self.assertIn("[learned: you said no to files.find for these words; answered as chat]", transcript)
        self.assertIn("4 lessons; for the same words, the latest counts:", transcript)
        self.assertEqual(3, len(self.marker.read_text().splitlines()))
        self.assertEqual([("apps.open", True), ("apps.open", False), ("apps.open", False)],
                         [(e["capability"]["name"], e["owner_confirmed"]) for e in _events(self.state / "events.jsonl")])

        out = self.base / "mine.lessons.tsv"
        report = self._run(["--config", str(self.config), "--export-lessons", str(out)])
        self.assertEqual("apps.open\tstart probe\tO B-app\nchat\tgood morning\tO O\n", out.read_text(encoding="utf-8"))
        self.assertIn('not written: "find chapter" (not)', report)
        self.assertIn("not written: \"play my song\" (do): the words do not contain the app 'probe'", report)
        self.assertIn(f"wrote 2 training lines from 4 lessons to {out}", report)

        # Never overwritten; the name must be one .gitignore lists; a broken log is refused.
        self.assertIn("already exists", self._run(["--config", str(self.config), "--export-lessons", str(out)], status=2))
        bad = self.base / "mine.tsv"
        self.assertIn("must end in .lessons.tsv", self._run(["--config", str(self.config), "--export-lessons", str(bad)],
                                                             status=2))
        self.assertFalse(bad.exists())
        log = self.state / "lessons.jsonl"
        log.write_text(log.read_text(encoding="utf-8").replace("good morning", "good evening"), encoding="utf-8")
        self.assertIn("lesson chain hash mismatch", self._run(
            ["--config", str(self.config), "--export-lessons", str(self.base / "again.lessons.tsv")], status=2))

    def test_the_same_session_with_learning_off_writes_no_lesson(self) -> None:
        self.config.write_text(self.text.replace("enabled = true", "enabled = false"), encoding="utf-8")
        transcript = self._session(1)
        self.assertIn(LEARNING_OFF, transcript)
        self.assertNotIn("[learned", transcript)
        self.assertEqual([], list(self.state.glob("lessons*")))


class VoiceLessonTests(unittest.TestCase):
    """listener -> wake word -> lesson or NOVA's command model -> broker -> speaker."""

    CANARY = "CANARY-OVERHEARD-58"

    def test_a_spoken_yes_is_learned_and_an_overheard_line_is_not(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        marker, spoken = base / "spotify-runs", base / "spoken.txt"
        room = tempfile.TemporaryDirectory()  # outside the scanned folder: what the room said
        self.addCleanup(room.cleanup)
        heard = Path(room.name) / "heard.txt"
        heard.write_text("\n".join([
            f"my sister said {self.CANARY} on the phone", "nova open spotify", "yes",
            "Nova, open Spotify please", f"open spotify {self.CANARY}",
        ]) + "\n", encoding="utf-8")
        listener = [sys.executable, "-c", f"print(open({str(heard)!r}, encoding='utf-8').read(), end='')"]
        speaker = [sys.executable, "-c",
                   f"import sys; open({str(spoken)!r}, 'a', encoding='utf-8').write(sys.stdin.read() + '\\n')"]
        app = [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('ran\\n')"]
        router = [sys.executable, "-I", str(ROOT / "novacore" / "src" / "novacore" / "intent.py"),
                  "--model", str(ROOT / "intent" / "model" / "nova-intent.bin"), "{schema_file}"]
        config = base / "config.toml"
        config.write_text(
            "[model]\n"
            f"command = {json.dumps([sys.executable, str(HERE / 'fake_runner.py')])}\n"
            "timeout_seconds = 10\nmax_output_chars = 1000\nuse_stdin = true\n\n"
            f"[router]\ncommand = {json.dumps(router)}\n"
            "timeout_seconds = 60\nmax_output_chars = 2000\nuse_stdin = true\n\n"
            f"[desktop]\nconfirm_all_actions = true\n\n[desktop.apps]\nspotify = {json.dumps(app)}\n\n"
            f"[voice]\nlistener = {json.dumps(listener)}\nspeaker = {json.dumps(speaker)}\n\n"
            "[conversation]\nhistory_turns = 2\nmemory_results = 2\n\n"
            "[storage]\nstate_dir = \"state\"\n\n[learning]\nenabled = true\n",
            encoding="utf-8",
        )
        self.assertEqual(0, cli.main(["--config", str(config), "--voice"]))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (not marker.exists() or len(marker.read_text().splitlines()) < 2):
            time.sleep(0.05)
        from novacore import desktop
        for child in list(desktop._LAUNCHED):
            child.wait(timeout=10)
        self.assertEqual(2, len(marker.read_text().splitlines()))
        said = spoken.read_text(encoding="utf-8")
        self.assertIn("(apps.open). Type yes to do it", said)
        self.assertIn('Started "spotify".\n[learned: next time these words run at once]', said)
        self.assertIn('Started "spotify". [learned]', said)
        records = _records(base / "state" / "lessons.jsonl")
        self.assertEqual([("do", "open spotify", "apps.open")], [(r["verdict"], r["text"], r["capability"]) for r in records])
        # The overheard canary reached no file, the lesson log included.
        for path in base.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.CANARY, path.read_text(encoding="utf-8", errors="replace"), str(path))


class LearningConfigTests(unittest.TestCase):
    def _load(self, table: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(
            '[model]\ncommand = ["/x"]\ntimeout_seconds = 1\nmax_output_chars = 1\n\n'
            '[conversation]\nhistory_turns = 1\nmemory_results = 1\n\n[storage]\nstate_dir = "s"\n\n' + table,
            encoding="utf-8",
        )
        return load_config(path)

    def test_off_unless_turned_on_and_strictly_typed(self) -> None:
        self.assertEqual((False, True), (self._load("").learning.enabled, self._load("").learning.trust_learned))
        learning = self._load("[learning]\nenabled = true\ntrust_learned = false\n").learning
        self.assertEqual((True, False), (learning.enabled, learning.trust_learned))
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            self._load("[learning]\nenabled = true\nupload = true\n")
        with self.assertRaisesRegex(ConfigError, "learning.enabled must be true or false"):
            self._load('[learning]\nenabled = "yes"\n')


if __name__ == "__main__":
    unittest.main()
