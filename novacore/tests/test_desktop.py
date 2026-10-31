"""N1: the desktop bridge and the broker path that runs it.

Oracles:
- A recording fake platform: the tests assert which calls reached the
  computer, written from the work order's rules, not from desktop.py.
- A real folder tree with symlinks that point outside the listed folder, a
  sibling folder whose name starts with the listed folder's name (the prefix
  trick), hidden files and executable types.
- The audit log is re-checked with a chain function written here from the
  format stated in the capabilities.py docstring (as test_outbox.py does),
  not with chainlog.py.
- The Linux helper argv lists are written from the tools' manual pages:
  pactl(1) `set-sink-volume @DEFAULT_SINK@ +5%`, `set-sink-mute ... toggle`;
  playerctl(1) `play-pause`, `next`, `previous`; loginctl(1) `lock-session`;
  xdg-open(1) `xdg-open {file}`.
- 2026-10-31 being a Saturday is checked against `cal 10 2026` in
  docs/logs/n1-evidence.log, not against Python's calendar.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from novacore.capabilities import CapabilityBroker, argument_problem
from novacore.config import DesktopConfig
from novacore.desktop import MAX_TIMERS, DesktopBridge, LinuxPlatform, PlatformError
from novacore.memory import MemoryStore


class FakePlatform:
    name = "fake"

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    def _record(self, *call: object) -> None:
        if self.fail:
            raise PlatformError(self.fail)
        self.calls.append(tuple(call))

    def launch(self, argv: tuple[str, ...]) -> None:
        self._record("launch", tuple(argv))

    def open_file(self, path: Path) -> None:
        self._record("open_file", str(path))

    def media(self, action: str) -> None:
        self._record("media", action)

    def volume(self, action: str) -> None:
        self._record("volume", action)

    def lock(self) -> None:
        self._record("lock")


def _chain(fields: dict, previous: str) -> str:
    """The outbox chain as the capabilities.py docstring states it."""
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((canonical + previous).encode("utf-8")).hexdigest()


def _events(path: Path) -> list[dict]:
    """Read the outbox and check every link independently of chainlog.py."""
    previous = "0" * 64
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        fields = {key: value for key, value in event.items() if key != "chain_hash"}
        if _chain(fields, previous) != event["chain_hash"]:
            raise AssertionError(f"chain broken at sequence {event['sequence']}")
        previous = event["chain_hash"]
        events.append(event)
    return events


class Tree:
    """docs/ is listed; docs2/ and outside/ are not."""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        base = Path(self._directory.name)
        self.base = base
        self.docs = base / "docs"
        (self.docs / "physics").mkdir(parents=True)
        (self.docs / ".hidden").mkdir()
        (base / "docs2").mkdir()
        (base / "outside").mkdir()
        (base / "music").mkdir()
        for name in (
            "physics/notes chapter3.pdf",
            "physics/notes chapter4.pdf",
            "notes.txt",
            "budget 2026.xlsx",
            "run.sh",
            "report.html",
            "drawing.svg",
            "setup.exe",
            ".secret.pdf",
            ".hidden/inside.pdf",
        ):
            (self.docs / name).write_text("x", encoding="utf-8")
        (base / "docs2" / "lure.pdf").write_text("x", encoding="utf-8")
        (base / "outside" / "private.pdf").write_text("x", encoding="utf-8")
        (base / "music" / "song one.mp3").write_text("x", encoding="utf-8")
        self.symlinks = sys.platform != "win32"
        if self.symlinks:
            os.symlink(base / "outside" / "private.pdf", self.docs / "escape.pdf")
            os.symlink(base / "outside", self.docs / "linked_dir")
            os.symlink(base / "docs2" / "lure.pdf", self.docs / "prefix.pdf")

    def real(self, relative: str) -> str:
        return os.path.normcase(os.path.realpath(self.docs / relative))

    def cleanup(self) -> None:
        self._directory.cleanup()


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = Tree()
        self.addCleanup(self.tree.cleanup)
        self.platform = FakePlatform()
        self.memory = MemoryStore(self.tree.base / "state" / "memories.jsonl")
        self.notices: list[str] = []
        self.timers: list[tuple[float, object]] = []
        self.program = self.tree.base / "music-player"
        self.program.write_text("", encoding="utf-8")
        self.config = DesktopConfig(
            apps={"music": (str(self.program), "--quiet"), "gone": (str(self.tree.base / "missing-program"),)},
            folders=(self.tree.docs, self.tree.base / "music"),
            confirm_all_actions=False,
        )
        self.bridge = self._bridge(self.platform)

    def _bridge(self, platform: object) -> DesktopBridge:
        return DesktopBridge(
            self.config,
            platform,  # type: ignore[arg-type]
            memory=self.memory,
            notify=self.notices.append,
            clock=lambda: datetime(2026, 10, 31, 10, 55, tzinfo=UTC),
            start_timer=lambda seconds, callback: self.timers.append((seconds, callback)),
        )


class FileConfinementTests(BridgeTestCase):
    def _found(self, query: str) -> str:
        result = self.bridge.run("files.find", {"query": query})
        self.assertTrue(result.ok)
        return result.message

    def test_find_lists_matches_inside_the_folders_only(self) -> None:
        message = self._found("notes")
        self.assertIn("docs/physics/notes chapter3.pdf", message)
        self.assertIn("docs/physics/notes chapter4.pdf", message)
        self.assertIn("docs/notes.txt", message)
        self.assertTrue(message.startswith("Found 3:"), message)

    def test_hidden_files_and_folders_are_skipped(self) -> None:
        self.assertIn("No file", self._found("secret"))
        self.assertIn("No file", self._found("inside"))

    @unittest.skipUnless(sys.platform != "win32", "symlinks need privileges on Windows")
    def test_symlinks_out_of_the_folder_are_never_listed(self) -> None:
        for query in ("escape", "private", "linked_dir", "prefix", "lure"):
            with self.subTest(query):
                self.assertIn("No file", self._found(query))

    def test_a_path_in_the_query_is_only_words(self) -> None:
        for query in ("../outside/private.pdf", "../docs2/lure", str(self.tree.base / "outside")):
            with self.subTest(query):
                self.assertIn("No file", self._found(query))
                self.assertEqual([], self.platform.calls)

    def test_open_hands_one_matching_document_to_the_platform(self) -> None:
        result = self.bridge.run("files.open", {"query": "chapter3"})
        self.assertTrue(result.ok, result.message)
        self.assertEqual([("open_file", self.tree.real("physics/notes chapter3.pdf"))], self.platform.calls)

    def test_open_prefers_the_one_file_whose_name_is_exactly_the_query(self) -> None:
        result = self.bridge.run("files.open", {"query": "notes"})
        self.assertTrue(result.ok, result.message)
        self.assertEqual([("open_file", self.tree.real("notes.txt"))], self.platform.calls)

    def test_open_with_several_matches_opens_nothing_and_lists_them(self) -> None:
        result = self.bridge.run("files.open", {"query": "chapter"})
        self.assertFalse(result.ok)
        self.assertIn("Several files match", result.message)
        self.assertIn("notes chapter3.pdf", result.message)
        self.assertEqual([], self.platform.calls)

    def test_programs_scripts_and_web_pages_are_never_opened(self) -> None:
        for query in ("run", "report", "drawing", "setup"):
            with self.subTest(query):
                result = self.bridge.run("files.open", {"query": query})
                self.assertFalse(result.ok)
                self.assertIn("none is a document or media type", result.message)
        self.assertEqual([], self.platform.calls)

    @unittest.skipUnless(sys.platform != "win32", "symlinks need privileges on Windows")
    def test_open_refuses_symlinks_out_of_the_folder(self) -> None:
        for query in ("escape", "prefix", "private"):
            with self.subTest(query):
                self.assertFalse(self.bridge.run("files.open", {"query": query}).ok)
        self.assertEqual([], self.platform.calls)

    @unittest.skipUnless(sys.platform != "win32", "symlinks need privileges on Windows")
    def test_open_rechecks_a_hit_that_became_a_link(self) -> None:
        # The search returned a path; by open time it is a link to outside.
        link = str(self.tree.docs / "escape.pdf")
        self.bridge._search = lambda query: ([(link, "docs/escape.pdf")], False)  # type: ignore[method-assign]
        result = self.bridge.run("files.open", {"query": "escape"})
        self.assertFalse(result.ok)
        self.assertIn("changed since the search", result.message)
        self.assertEqual([], self.platform.calls)

    def test_a_listed_folder_given_through_a_link_still_works(self) -> None:
        if sys.platform == "win32":
            self.skipTest("symlinks need privileges on Windows")
        alias = self.tree.base / "docs-alias"
        os.symlink(self.tree.docs, alias)
        config = DesktopConfig(apps={}, folders=(alias,), confirm_all_actions=False)
        bridge = DesktopBridge(config, self.platform, memory=None)
        self.assertTrue(bridge.run("files.open", {"query": "chapter4"}).ok)
        self.assertEqual([("open_file", self.tree.real("physics/notes chapter4.pdf"))], self.platform.calls)


class OtherActionTests(BridgeTestCase):
    def test_apps_open_starts_exactly_the_configured_argv(self) -> None:
        result = self.bridge.run("apps.open", {"app": "music"})
        self.assertTrue(result.ok)
        self.assertEqual([("launch", (str(self.program), "--quiet"))], self.platform.calls)

    def test_apps_open_reports_a_missing_program_and_starts_nothing(self) -> None:
        result = self.bridge.run("apps.open", {"app": "gone"})
        self.assertFalse(result.ok)
        self.assertIn("was not found", result.message)
        self.assertEqual([], self.platform.calls)

    def test_media_volume_and_lock_reach_the_platform(self) -> None:
        self.bridge.run("media.control", {"action": "next"})
        self.bridge.run("volume.control", {"action": "mute"})
        self.bridge.run("system.lock", {})
        self.assertEqual([("media", "next"), ("volume", "mute"), ("lock",)], self.platform.calls)

    def test_time_now_reads_the_clock(self) -> None:
        message = self.bridge.run("time.now", {}).message
        self.assertEqual("It is 10:55 on Saturday 31 October 2026 (UTC).", message)

    def test_timer_waits_the_minutes_asked_and_then_announces_the_label(self) -> None:
        result = self.bridge.run("timer.set", {"minutes": 10, "label": "stretch"})
        self.assertTrue(result.ok)
        self.assertEqual([600.0], [seconds for seconds, _ in self.timers])
        self.assertEqual([], self.notices)
        self.timers[0][1]()  # the timer fires
        self.assertEqual(["Timer: stretch"], self.notices)

    def test_running_timers_are_capped(self) -> None:
        for _ in range(MAX_TIMERS):
            self.assertTrue(self.bridge.run("timer.set", {"minutes": 1, "label": ""}).ok)
        self.assertFalse(self.bridge.run("timer.set", {"minutes": 1, "label": ""}).ok)
        self.timers[0][1]()  # one fires, freeing a slot
        self.assertTrue(self.bridge.run("timer.set", {"minutes": 1, "label": ""}).ok)

    def test_memory_save_writes_a_note_another_store_can_read(self) -> None:
        self.assertTrue(self.bridge.run("memory.save", {"text": "exam on Monday"}).ok)
        reader = MemoryStore(self.tree.base / "state" / "memories.jsonl")
        self.assertEqual(["exam on Monday"], [memory.text for memory in reader.search("exam", 5)])

    def test_availability_follows_the_platform_and_the_configuration(self) -> None:
        self.assertEqual(
            {"apps.open", "files.find", "files.open", "media.control", "volume.control",
             "system.lock", "time.now", "timer.set", "memory.save"},
            set(self.bridge.available()),
        )
        no_platform = DesktopBridge(self.config, None, memory=None)
        self.assertEqual({"files.find", "time.now", "timer.set"}, set(no_platform.available()))
        empty = DesktopBridge(DesktopConfig({}, (), False), self.platform, memory=None)
        self.assertEqual(
            {"media.control", "volume.control", "system.lock", "time.now", "timer.set"},
            set(empty.available()),
        )

    def test_every_example_shown_to_the_router_obeys_its_own_rules(self) -> None:
        for action in self.bridge.action_specs():
            with self.subTest(action.name):
                self.assertIsNone(argument_problem(action.args, dict(action.example_args)))


class BrokerRunTests(BridgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.events = self.tree.base / "state" / "events.jsonl"
        self.broker = CapabilityBroker(self.events, self.bridge)

    def test_low_risk_action_runs_and_is_logged(self) -> None:
        result = self.broker.request("volume.control", {"action": "up"})
        self.assertEqual("done", result.state)
        self.assertEqual([("volume", "up")], self.platform.calls)
        [event] = _events(self.events)
        self.assertEqual(("CAPABILITY_RUN", True, False), (event["kind"], event["applied"], event["owner_confirmed"]))
        self.assertEqual({"action": "up"}, event["arguments"])

    def test_memory_save_needs_confirmation_and_writes_nothing_without_it(self) -> None:
        result = self.broker.request("memory.save", {"text": "exam on Monday"})
        self.assertEqual("confirmation_required", result.state)
        self.assertFalse(self.events.exists())
        self.assertFalse((self.tree.base / "state" / "memories.jsonl").exists())
        result = self.broker.request("memory.save", {"text": "exam on Monday"}, owner_confirmed=True)
        self.assertEqual("done", result.state)
        [event] = _events(self.events)
        self.assertTrue(event["owner_confirmed"])

    def test_arguments_outside_the_rules_are_denied_before_the_platform(self) -> None:
        for name, arguments in (
            ("apps.open", {"app": "calc"}),
            ("apps.open", {"app": "music", "argv": ["/bin/sh"]}),
            ("volume.control", {"action": "max"}),
            ("timer.set", {"minutes": 0, "label": ""}),
            ("files.open", {"query": ""}),
        ):
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual("denied", self.broker.request(name, arguments).state)
        self.assertEqual([], self.platform.calls)
        self.assertFalse(self.events.exists())

    def test_without_a_bridge_desktop_actions_are_unavailable(self) -> None:
        result = CapabilityBroker(self.events).request("system.lock", {})
        self.assertEqual("unavailable", result.state)
        self.assertFalse(self.events.exists())

    def test_a_platform_failure_is_logged_as_not_applied(self) -> None:
        broker = CapabilityBroker(self.events, self._bridge(FakePlatform(fail="pactl is not installed")))
        result = broker.request("volume.control", {"action": "up"})
        self.assertEqual("failed", result.state)
        self.assertIn("pactl is not installed", result.message)
        [event] = _events(self.events)
        self.assertFalse(event["applied"])

    def test_nothing_runs_on_an_outbox_that_does_not_verify(self) -> None:
        self.broker.request("system.lock", {})
        lines = self.events.read_text(encoding="utf-8")
        self.events.write_text(lines.replace('"applied": true', '"applied": false'), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.broker.request("system.lock", {})
        self.assertEqual([("lock",)], self.platform.calls)  # only the first one


class LinuxPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ran: list[list[str]] = []
        self.started: list[tuple[list[str], dict]] = []
        self.exit_status = 0

        def run(argv, **options):
            self.ran.append(argv)
            return subprocess.CompletedProcess(argv, self.exit_status, b"", b"no player")

        def popen(argv, **options):
            self.started.append((argv, options))

        self.linux = LinuxPlatform(which=lambda name: f"/usr/bin/{name}", run=run, popen=popen)

    def test_helper_argv_match_the_manual_pages(self) -> None:
        self.linux.volume("up")
        self.linux.volume("down")
        self.linux.volume("mute")
        self.linux.media("play_pause")
        self.linux.media("previous")
        self.linux.lock()
        self.assertEqual(
            [
                ["/usr/bin/pactl", "set-sink-volume", "@DEFAULT_SINK@", "+5%"],
                ["/usr/bin/pactl", "set-sink-volume", "@DEFAULT_SINK@", "-5%"],
                ["/usr/bin/pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                ["/usr/bin/playerctl", "play-pause"],
                ["/usr/bin/playerctl", "previous"],
                ["/usr/bin/loginctl", "lock-session"],
            ],
            self.ran,
        )

    def test_open_file_detaches_xdg_open_with_the_absolute_path(self) -> None:
        self.linux.open_file(Path("/home/owner/docs/a.pdf"))
        [(argv, options)] = self.started
        self.assertEqual(["/usr/bin/xdg-open", "/home/owner/docs/a.pdf"], argv)
        self.assertTrue(options["start_new_session"])
        self.assertNotIn("shell", options)

    def test_a_missing_tool_or_a_failing_one_is_reported_truthfully(self) -> None:
        missing = LinuxPlatform(which=lambda name: None)
        with self.assertRaisesRegex(PlatformError, "pactl is not installed"):
            missing.volume("up")
        self.exit_status = 1
        with self.assertRaisesRegex(PlatformError, "playerctl exited with status 1: no player"):
            self.linux.media("next")

    @unittest.skipUnless(sys.platform.startswith("linux"), "real launch is checked on Linux only")
    def test_real_launch_starts_the_program(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started"
            LinuxPlatform().launch((sys.executable, "-c", f"open({str(marker)!r}, 'w').write('yes')"))
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "the launched program never ran")


if __name__ == "__main__":
    unittest.main()
