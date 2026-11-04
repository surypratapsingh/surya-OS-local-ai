"""D2: verify that no network sockets are opened during a normal NovaCore session.

Audit hook oracle: CPython's sys.addaudithook monitors low-level interpreter events
such as socket.__new__, socket.connect, socket.bind, and socket.getaddrinfo.

Documented limit:
The audit hook inspects only the Python interpreter running NovaCore.
The local model runner (when using LocalCommandBackend / SubprocessModelRunner)
executes as a separate external child process chosen and configured by the owner;
any network activity within that external process is outside the scope of this
in-process Python audit test.
"""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from novacore import cli
from novacore.capabilities import CapabilityBroker
from novacore.companion import Companion
from novacore.config import load_config
from novacore.demand import DemandStore
from novacore.memory import MemoryStore
from novacore.shuttle import ShuttleImporter, ShuttleVerificationError
from novacore.updates import UpdateProposalStore


_recording = False
_recorded_socket_events: list[tuple[str, tuple]] = []


def _audit_hook(event: str, args: tuple) -> None:
    if _recording and event.startswith("socket"):
        _recorded_socket_events.append((event, args))


# Register audit hook once at module import. In CPython, audit hooks cannot be removed.
sys.addaudithook(_audit_hook)


@contextmanager
def record_socket_events():
    global _recording
    _recorded_socket_events.clear()
    _recording = True
    try:
        yield _recorded_socket_events
    finally:
        _recording = False


class FakeModelRunner:
    def complete(self, prompt: str) -> str:
        return "I am NovaCore, running strictly local."


class NoSocketsTests(unittest.TestCase):
    def test_normal_session_opens_zero_sockets(self) -> None:
        """Run standard session workflow and assert zero socket events occur."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_file = temp_path / "config.toml"
            config_file.write_text(
                "[model]\n"
                'command = ["fake-runner"]\n'
                "timeout_seconds = 5\n"
                "max_output_chars = 1000\n"
                "use_stdin = true\n\n"
                "[conversation]\n"
                "history_turns = 2\n"
                "memory_results = 2\n\n"
                "[storage]\n"
                'state_dir = "state"\n',
                encoding="utf-8",
            )

            with record_socket_events() as events:
                # 1. Load config
                config = load_config(config_file)
                self.assertTrue(config.state_dir.is_relative_to(temp_path))

                # 2. Setup memory and take a full Companion turn with fake runner
                memory_path = config.state_dir / "memories.jsonl"
                memory = MemoryStore(memory_path)
                fake_runner = FakeModelRunner()
                companion = Companion(fake_runner, memory, config.conversation)

                # 3. Add and search a memory
                saved = companion.remember("Owner prefers offline operations.")
                self.assertIsNotNone(saved.memory_id)
                search_results = companion.memory.search("offline", limit=5)
                self.assertEqual(len(search_results), 1)

                # Take a full turn
                reply = companion.reply("What are my preferences?")
                self.assertEqual(reply, "I am NovaCore, running strictly local.")

                # 4. Request a capability
                event_path = config.state_dir / "events.jsonl"
                broker = CapabilityBroker(event_path)
                status = broker.status()
                self.assertEqual(status.state, "observed")
                status_res = broker.request("system.status", {})
                self.assertEqual(status_res.state, "observed")
                reboot_res = broker.request("system.reboot", {})
                self.assertEqual(reboot_res.state, "confirmation_required")

                # 5. Write an update proposal
                proposals_dir = config.state_dir / "proposals"
                proposal_store = UpdateProposalStore(proposals_dir)
                prop_path = proposal_store.create("Safe offline update", "Zero socket proposal.")
                self.assertTrue(prop_path.exists())

                # Assert zero socket events
                self.assertEqual(
                    events,
                    [],
                    f"Expected 0 socket events during normal session, but recorded: {events}",
                )

    def test_audit_hook_detects_socket_events_when_recording(self) -> None:
        """Verify the audit hook oracle detects socket operations when active."""
        import socket

        with record_socket_events() as events:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.close()

        self.assertGreater(len(events), 0)
        event_names = [e[0] for e in events]
        self.assertIn("socket.__new__", event_names)

    def test_cli_session_opens_zero_sockets(self) -> None:
        """Verify cli.main session with LocalCommandBackend opens zero sockets (G1)."""
        fake_runner = Path(__file__).parent / "fake_runner.py"
        self.assertTrue(fake_runner.exists(), f"fake runner script missing: {fake_runner}")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_file = temp_path / "config.toml"
            runner_cmd = json.dumps([sys.executable, str(fake_runner)])
            config_file.write_text(
                "[model]\n"
                f"command = {runner_cmd}\n"
                "timeout_seconds = 5\n"
                "max_output_chars = 1000\n"
                "use_stdin = true\n\n"
                "[conversation]\n"
                "history_turns = 2\n"
                "memory_results = 2\n\n"
                "[storage]\n"
                'state_dir = "state"\n',
                encoding="utf-8",
            )

            stdout_buf = io.StringIO()
            with record_socket_events() as events:
                with patch("sys.stdout", stdout_buf):
                    exit_code = cli.main(["--config", str(config_file), "--message", "hello"])

            self.assertEqual(exit_code, 0)
            self.assertIn("strictly local", stdout_buf.getvalue())
            self.assertEqual(
                events,
                [],
                f"Expected 0 socket events during CLI session, but recorded: {events}",
            )

    def test_cli_interactive_session_opens_zero_sockets(self) -> None:
        """Verify cli.main interactive loop with slash commands opens zero sockets (G2)."""
        fake_runner = Path(__file__).parent / "fake_runner.py"
        self.assertTrue(fake_runner.exists(), f"fake runner script missing: {fake_runner}")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_file = temp_path / "config.toml"
            runner_cmd = json.dumps([sys.executable, str(fake_runner)])
            config_file.write_text(
                "[model]\n"
                f"command = {runner_cmd}\n"
                "timeout_seconds = 5\n"
                "max_output_chars = 1000\n"
                "use_stdin = true\n\n"
                "[conversation]\n"
                "history_turns = 2\n"
                "memory_results = 2\n\n"
                "[storage]\n"
                'state_dir = "state"\n',
                encoding="utf-8",
            )

            export_dir = temp_path / "shuttle_export"
            inputs = [
                "/remember offline preference",
                "/mem offline",
                "/status",
                "/demand",
                "/demand add Matrix pack | Need class 11 | https://example.org/m.pkg | Download; Copy",
                "/demand",
                f"/demand export {export_dir}",
                "hello",
                "/quit",
            ]
            stdout_buf = io.StringIO()
            with record_socket_events() as events:
                with patch("builtins.input", side_effect=inputs), patch("sys.stdout", stdout_buf):
                    exit_code = cli.main(["--config", str(config_file)])

            self.assertEqual(exit_code, 0)
            output = stdout_buf.getvalue()
            self.assertIn("NOVA local companion", output)
            self.assertIn("Saved memory", output)
            self.assertIn("Created demand entry", output)
            self.assertIn("Exported 1 open demand(s)", output)
            self.assertIn("strictly local", output)
            self.assertEqual(
                events,
                [],
                f"Expected 0 socket events during interactive CLI session, but recorded: {events}",
            )

    def test_demand_and_shuttle_open_zero_sockets(self) -> None:
        """Verify DemandStore and ShuttleImporter open zero network sockets."""
        _tools = Path(__file__).resolve().parent.parent.parent / "tools"
        if str(_tools) not in sys.path:
            sys.path.insert(0, str(_tools))
        import importlib.util
        spec = importlib.util.spec_from_file_location("pack_shuttle", _tools / "pack-shuttle.py")
        pack_shuttle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pack_shuttle)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            demands_path = temp_path / "demands.jsonl"
            export_path = temp_path / "export"
            proposals_dir = temp_path / "proposals"
            proposals = UpdateProposalStore(proposals_dir)

            key_dir = temp_path / "keys"
            priv_path, pub_path = pack_shuttle.ensure_test_key(key_dir)

            # Package source
            src_file = temp_path / "payload.bin"
            src_file.write_bytes(b"offline shuttle bytes " * 16)
            pkg_dir = temp_path / "pkg"
            pack_shuttle.pack_shuttle(
                out_dir=pkg_dir,
                packages=[("shuttle-mod", "1.0.0", src_file, ["test:run"])],
                sequence=1,
                version="1.0.0",
                key_dir=key_dir,
            )

            with record_socket_events() as events:
                # 1. DemandStore operations
                store = DemandStore(demands_path)
                entry = store.add(
                    title="Maths pack",
                    description="Need matrices notes",
                    urls=["https://example.org/pack.pkg"],
                    instructions=["Download on internet PC", "Copy to pendrive"],
                )
                store.list()
                store.get(entry.entry_id)
                store.export(export_path)
                store.resolve(entry.entry_id)

                # 2. ShuttleImporter operations
                importer = ShuttleImporter(proposals, trusted_public_key=pub_path)
                prop = importer.import_package(pkg_dir)
                self.assertTrue(prop.exists())

                # 3. Refused package operation
                bad_dir = temp_path / "bad_pkg"
                bad_dir.mkdir()
                with self.assertRaises(ShuttleVerificationError):
                    importer.import_package(bad_dir)

            self.assertEqual(
                events,
                [],
                f"Expected 0 socket events in demand and shuttle operations, got: {events}",
            )


if __name__ == "__main__":
    unittest.main()

