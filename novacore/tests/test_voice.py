"""N2: always-on voice. NovaCore reads text lines from a listener program and
acts only on lines addressed to it.

Oracles: wake-word cases written by hand; a full CLI run with real listener and
speaker subprocesses (scripted stand-ins for System.Speech, not speech
engines), with NOVA's own command model as the router; and a canary sentence
spoken "in the room" without the wake word, searched for afterwards in every
file the run could have written.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from novacore import cli
from novacore.config import ConfigError, load_config
from novacore.voice import DEFAULT_WAKE_WORDS, VoiceLoop, strip_wake_word

ROOT = Path(__file__).resolve().parents[2]


class WakeWordTests(unittest.TestCase):
    def test_wake_words(self) -> None:
        cases = {
            "Nova, open Spotify": "open Spotify",
            "hey nova what time is it": "what time is it",
            "OK Nova. lock the screen": "lock the screen",
            "nova": "",
            "okay nova": "",
            "novak said hello": None,
            "I told nova yesterday": None,
            "supernova facts": None,
            "": None,
        }
        for line, command in cases.items():
            with self.subTest(line):
                self.assertEqual(command, strip_wake_word(line, DEFAULT_WAKE_WORDS))


class StepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handled: list[str] = []
        self.pending = False
        self.loop = VoiceLoop(lambda c: self.handled.append(c) or f"did {c}", lambda: self.pending, ["/unused"])

    def test_only_addressed_lines_reach_the_assistant(self) -> None:
        self.assertIsNone(self.loop.step("pass me the salt"))
        self.assertEqual("did open notepad", self.loop.step("Nova open notepad"))
        self.assertEqual("Yes?", self.loop.step("hey nova"))
        self.assertEqual(["open notepad"], self.handled)

    def test_while_a_confirmation_waits_the_next_line_is_the_answer(self) -> None:
        self.pending = True
        self.assertEqual("did yes", self.loop.step("yes"))
        self.assertEqual(["yes"], self.handled)


class CliVoiceTests(unittest.TestCase):
    """The whole path: listener -> wake word -> router model -> broker -> bridge -> speaker."""

    CANARY = "CANARY-OVERHEARD-31"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.marker = self.base / "spotify-started"
        self.spoken = self.base / "spoken.txt"
        lines = [f"my brother said {self.CANARY} at dinner", "nova open spotify", "Nova, what time is it",
                 "nova remember that the gate code is 4321", "yes"]
        # What the room "says" lives outside the scanned run folder, so only
        # files NovaCore itself wrote are searched for the overheard canary.
        room = tempfile.TemporaryDirectory()
        self.addCleanup(room.cleanup)
        heard = Path(room.name) / "heard.txt"
        heard.write_text("\n".join(lines) + "\n", encoding="utf-8")
        listener = [sys.executable, "-c", f"print(open({str(heard)!r}, encoding='utf-8').read(), end='')"]
        speaker = [sys.executable, "-c", f"import sys; open({str(self.spoken)!r}, 'a', encoding='utf-8').write(sys.stdin.read() + '\\n')"]
        app = [sys.executable, "-c", f"open({str(self.marker)!r}, 'w').write('ran')"]
        here = Path(__file__).parent
        router = [sys.executable, "-I", str(ROOT / "novacore" / "src" / "novacore" / "intent.py"),
                  "--model", str(ROOT / "intent" / "model" / "nova-intent.bin"), "{schema_file}"]
        self.config = self.base / "config.toml"
        self.config.write_text(
            "[model]\n"
            f"command = {json.dumps([sys.executable, str(here / 'fake_runner.py')])}\n"
            "timeout_seconds = 10\nmax_output_chars = 1000\nuse_stdin = true\n\n"
            "[router]\n"
            f"command = {json.dumps(router)}\n"
            "timeout_seconds = 60\nmax_output_chars = 2000\nuse_stdin = true\n\n"
            "[desktop]\nconfirm_all_actions = false\n\n[desktop.apps]\n"
            f"spotify = {json.dumps(app)}\n\n"
            "[voice]\n"
            f"listener = {json.dumps(listener)}\n"
            f"speaker = {json.dumps(speaker)}\n\n"
            "[conversation]\nhistory_turns = 2\nmemory_results = 2\n\n"
            "[storage]\nstate_dir = \"state\"\n",
            encoding="utf-8",
        )

    def test_voice_session_end_to_end(self) -> None:
        self.assertEqual(0, cli.main(["--config", str(self.config), "--voice"]))
        deadline = time.monotonic() + 10
        while not self.marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.marker.exists(), "'nova open spotify' did not start the configured program")
        from novacore import desktop  # let the launched stand-in app finish before the test ends
        for child in list(desktop._LAUNCHED):
            child.wait(timeout=10)
        spoken = self.spoken.read_text(encoding="utf-8")
        self.assertIn('Started "spotify".', spoken)
        self.assertIn("It is ", spoken)
        self.assertIn("(memory.save). Type yes to do it", spoken)
        self.assertIn("Saved note", spoken)
        notes = (self.base / "state" / "memories.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(notes))
        # The overheard line went nowhere: no file anywhere under the run's folder holds it.
        for path in self.base.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.CANARY, path.read_text(encoding="utf-8", errors="replace"), str(path))

    def test_a_missing_listener_is_reported(self) -> None:
        text = self.config.read_text(encoding="utf-8")
        bad = text.replace(f"listener = {json.dumps([sys.executable])[:-1]}", f"listener = [\"{self.base / 'no-such-listener'}\"")
        start = bad.index("listener = ")
        end = bad.index("\n", start)
        bad = bad[:start] + f"listener = {json.dumps([str(self.base / 'no-such-listener')])}" + bad[end:]
        self.config.write_text(bad, encoding="utf-8")
        self.assertEqual(2, cli.main(["--config", str(self.config), "--voice"]))


class SpeakerTests(unittest.TestCase):
    def test_a_failing_speaker_is_reported_once_and_the_loop_goes_on(self) -> None:
        shown: list[str] = []
        loop = VoiceLoop(lambda c: "ok", lambda: False, ["/unused"], [sys.executable, "-c", "raise SystemExit(3)"],
                         show=shown.append)
        loop.speak("one")
        loop.speak("two")
        self.assertEqual(1, sum("exited with status 3" in s for s in shown))


    def test_a_speaker_that_cannot_start_is_reported_not_raised(self) -> None:
        # Added after plant V3 (speaker start failure not caught) passed: the
        # test above only covered a speaker that exits with an error.
        shown: list[str] = []
        loop = VoiceLoop(lambda c: "ok", lambda: False, ["/unused"], ["/no/such/speaker-program"], show=shown.append)
        loop.speak("hello")
        self.assertEqual(1, sum("speaker program failed" in s for s in shown))


class VoiceConfigTests(unittest.TestCase):
    def _config(self, voice: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(
            '[model]\ncommand = ["/x"]\ntimeout_seconds = 1\nmax_output_chars = 1\n\n'
            "[conversation]\nhistory_turns = 1\nmemory_results = 1\n\n[storage]\nstate_dir = \"s\"\n\n" + voice,
            encoding="utf-8",
        )
        return path

    def test_programs_must_be_absolute_and_keys_known(self) -> None:
        with self.assertRaisesRegex(ConfigError, "absolute path"):
            load_config(self._config('[voice]\nlistener = ["powershell.exe"]\n'))
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            load_config(self._config('[voice]\nlistener = ["/bin/x"]\nmicrophone = "on"\n'))
        config = load_config(self._config('[voice]\nlistener = ["/bin/x"]\nwake_words = ["Jarvis"]\n'))
        self.assertEqual(("jarvis",), config.voice.wake_words)
        self.assertIsNone(config.voice.speaker)



class AnswerWordTests(unittest.TestCase):
    """N2b: what counts as the answer to a waiting offer (hand-written cases)."""

    def test_only_a_whole_line_of_one_answer_word(self) -> None:
        from novacore.assistant import answer_word

        cases = {"yes": "yes", "Yes.": "yes", "  YES!  ": "yes", "y": "yes", "No.": "no", "cancel": "no",
                 "yes please": None, "yes, but later": None, "nova yes": None, "yeah": None, "": None, "...": None}
        for line, expected in cases.items():
            with self.subTest(line):
                self.assertEqual(expected, answer_word(line))


class WaitingOfferTests(unittest.TestCase):
    """N2b, with a fake clock: while an offer waits, only yes or no gets through, until it expires."""

    def setUp(self) -> None:
        self.pending = False
        self.now = 0.0
        self.handled: list[str] = []
        self.expired = 0
        self.shown: list[str] = []

        def handle(command: str) -> str:
            self.handled.append(command)
            if command.startswith("remember"):
                self.pending = True
                return "NOVA would save this note. Type yes to do it, or no to cancel."
            self.pending = False
            return f"did {command}"

        def expire() -> None:
            self.expired += 1
            self.pending = False

        self.loop = VoiceLoop(handle, lambda: self.pending, ["/unused"], show=self.shown.append,
                              expire=expire, confirm_seconds=30, clock=lambda: self.now)
        self.loop.step("nova remember the gate code")
        self.assertTrue(self.pending)

    def test_an_overheard_line_is_dropped_and_the_offer_keeps_waiting(self) -> None:
        self.assertIsNone(self.loop.step("my brother says the match starts at six"))
        self.assertEqual(["remember the gate code"], self.handled)
        self.assertTrue(self.pending)
        self.assertEqual("did Yes.", self.loop.step("Yes."))

    def test_a_late_yes_confirms_nothing(self) -> None:
        self.now = 30.5
        self.assertIsNone(self.loop.step("yes"))  # expired first, then a bare "yes" is not for NOVA
        self.assertEqual(1, self.expired)
        self.assertFalse(self.pending)
        self.assertEqual(["remember the gate code"], self.handled)
        self.assertIn("expired", self.shown[-1])

    def test_a_yes_inside_the_window_still_counts(self) -> None:
        self.now = 29.0
        self.assertEqual("did yes", self.loop.step("yes"))
        self.assertEqual(0, self.expired)


@unittest.skipUnless(sys.platform.startswith("linux"), "the voice run starts real programs; checked on Linux")
class WaitingOfferCliTests(unittest.TestCase):
    """N2b through cli.main --voice with NOVA's command model as the router."""

    CANARY = "CANARY-OVERHEARD-WHILE-WAITING-64"

    def _run(self, script: str, confirm_seconds: int) -> tuple[str, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        room = tempfile.TemporaryDirectory()  # the listener's own text lives outside the scanned folder
        self.addCleanup(room.cleanup)
        listener_script = Path(room.name) / "listen.py"
        listener_script.write_text(script, encoding="utf-8")
        spoken = base / "spoken.txt"
        speaker = [sys.executable, "-c", f"import sys; open({str(spoken)!r}, 'a', encoding='utf-8').write(sys.stdin.read() + '\\n')"]
        router = [sys.executable, "-I", str(ROOT / "novacore" / "src" / "novacore" / "intent.py"),
                  "--model", str(ROOT / "intent" / "model" / "nova-intent.bin"), "{schema_file}"]
        here = Path(__file__).parent
        config = base / "config.toml"
        config.write_text(
            "[model]\n"
            f"command = {json.dumps([sys.executable, str(here / 'fake_runner.py')])}\n"
            "timeout_seconds = 10\nmax_output_chars = 1000\nuse_stdin = true\n\n"
            f"[router]\ncommand = {json.dumps(router)}\ntimeout_seconds = 60\nmax_output_chars = 2000\nuse_stdin = true\n\n"
            "[desktop]\nconfirm_all_actions = true\n\n"
            f"[voice]\nlistener = {json.dumps([sys.executable, str(listener_script)])}\n"
            f"speaker = {json.dumps(speaker)}\nconfirm_seconds = {confirm_seconds}\n\n"
            "[conversation]\nhistory_turns = 2\nmemory_results = 2\n\n[storage]\nstate_dir = \"state\"\n",
            encoding="utf-8",
        )
        shown = io.StringIO()
        with patch("sys.stdout", shown):
            self.assertEqual(0, cli.main(["--config", str(config), "--voice"]))
        return shown.getvalue(), base

    def test_an_overheard_line_while_an_offer_waits_reaches_nothing_and_cancels_nothing(self) -> None:
        lines = ["nova remember that the gate code is 4321", f"my friend said {self.CANARY} just now", "Yes."]
        shown, base = self._run(f"print({chr(10).join(lines)!r}, flush=True)\n", confirm_seconds=30)
        self.assertIn("(memory.save). Type yes to do it", shown)
        self.assertIn("Saved note", shown)  # the overheard line did not cancel the offer
        notes = (base / "state" / "memories.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(notes))
        for path in base.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.CANARY, path.read_text(encoding="utf-8", errors="replace"), str(path))

    def test_an_offer_left_waiting_expires_and_a_late_yes_confirms_nothing(self) -> None:
        script = ("import time\n"
                  "print('nova remember that the gate code is 4321', flush=True)\n"
                  "time.sleep(4)\n"
                  "print('yes', flush=True)\n"
                  "print('nova yes', flush=True)\n")
        shown, base = self._run(script, confirm_seconds=1)
        self.assertIn("(memory.save). Type yes to do it", shown)
        self.assertIn("it expired and nothing was done", shown)
        self.assertNotIn("Saved note", shown)
        self.assertFalse((base / "state" / "memories.jsonl").exists())


class ConfirmSecondsConfigTests(unittest.TestCase):
    def test_default_and_bounds(self) -> None:
        def load(extra: str):
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            path = Path(directory.name) / "config.toml"
            path.write_text(
                '[model]\ncommand = ["/x"]\ntimeout_seconds = 1\nmax_output_chars = 1\n\n'
                "[conversation]\nhistory_turns = 1\nmemory_results = 1\n\n[storage]\nstate_dir = \"s\"\n\n"
                '[voice]\nlistener = ["/bin/x"]\n' + extra,
                encoding="utf-8",
            )
            return load_config(path)

        self.assertEqual(30, load("").voice.confirm_seconds)
        self.assertEqual(5, load("confirm_seconds = 5\n").voice.confirm_seconds)
        for bad in ("0", "601", '"30"', "true", "2.5"):
            with self.subTest(bad), self.assertRaisesRegex(ConfigError, "confirm_seconds"):
                load(f"confirm_seconds = {bad}\n")


if __name__ == "__main__":
    unittest.main()
