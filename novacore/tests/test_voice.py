"""N2: always-on voice. NovaCore reads text lines from a listener program and
acts only on lines addressed to it.

Oracles: wake-word cases written by hand; a full CLI run with real listener and
speaker subprocesses (scripted stand-ins for System.Speech, not speech
engines), with NOVA's own command model as the router; and a canary sentence
spoken "in the room" without the wake word, searched for afterwards in every
file the run could have written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
