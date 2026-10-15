"""Test that owner content does not leak to disk outside NOVA state directory."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from novacore.companion import Companion
from novacore.config import ConversationConfig, ModelConfig, load_config
from novacore.llm import LocalCommandBackend, ModelUnavailable
from novacore.memory import MemoryStore


class LLMPrivacyTests(unittest.TestCase):
    """Verify that conversation content stays off-disk or in protected directories."""

    def test_no_prompt_leak_to_system_temp_via_stdin(self) -> None:
        """Canary: a stub model running via stdin should not leak content to system temp."""
        with tempfile.TemporaryDirectory() as state_dir_str:
            state_dir = Path(state_dir_str)
            unique_token = f"CANARY_{uuid.uuid4().hex}"
            prompt = f"This is a test prompt containing {unique_token}. Please echo it back."

            # Stub runner that echoes the input and exits cleanly.
            stub_script = self._write_stub_runner(state_dir, "echo_runner.py", success=True)

            config = ModelConfig(
                command=(sys.executable, str(stub_script)),
                timeout_seconds=10,
                max_output_chars=1000,
                use_stdin=True,
            )
            backend = LocalCommandBackend(config, state_dir)

            # Run one turn.
            try:
                reply = backend.complete(prompt)
            except ModelUnavailable:
                self.fail("Stub runner should not raise ModelUnavailable on success")

            # Scan for the token in forbidden locations.
            token_found = self._scan_for_token(unique_token)
            self.assertEqual(
                [],
                token_found,
                f"Unique token found in forbidden locations: {token_found}. "
                f"Prompt may have leaked to system temp or other unprotected directory.",
            )

    def test_no_prompt_leak_on_timeout(self) -> None:
        """Canary: a timed-out stub model should not leave prompt file behind."""
        with tempfile.TemporaryDirectory() as state_dir_str:
            state_dir = Path(state_dir_str)
            unique_token = f"CANARY_{uuid.uuid4().hex}"
            prompt = f"Prompt with token {unique_token} that will timeout."

            # Stub runner that sleeps longer than the timeout.
            stub_script = self._write_stub_runner(state_dir, "sleep_runner.py", timeout=True)

            config = ModelConfig(
                command=(sys.executable, str(stub_script)),
                timeout_seconds=1,
                max_output_chars=1000,
                use_stdin=False,
            )
            backend = LocalCommandBackend(config, state_dir)

            # Run and expect timeout.
            with self.assertRaises(ModelUnavailable) as cm:
                backend.complete(prompt)
            self.assertIn("timed out", str(cm.exception))

            # Verify no prompt file left behind in state_dir.
            prompt_dir = state_dir / "prompts"
            if prompt_dir.exists():
                remaining_files = list(prompt_dir.glob("prompt-*.txt"))
                self.assertEqual(
                    [],
                    remaining_files,
                    f"Prompt files left behind after timeout: {remaining_files}",
                )

            token_found = self._scan_for_token(unique_token)
            self.assertEqual([], token_found, f"Token found in forbidden locations after timeout: {token_found}")

    def test_no_prompt_leak_on_nonzero_exit(self) -> None:
        """Canary: a failing stub model should not leave prompt file behind."""
        with tempfile.TemporaryDirectory() as state_dir_str:
            state_dir = Path(state_dir_str)
            unique_token = f"CANARY_{uuid.uuid4().hex}"
            prompt = f"Prompt {unique_token} that will fail."

            # Stub runner that exits non-zero.
            stub_script = self._write_stub_runner(state_dir, "fail_runner.py", fail=True)

            config = ModelConfig(
                command=(sys.executable, str(stub_script)),
                timeout_seconds=10,
                max_output_chars=1000,
                use_stdin=False,
            )
            backend = LocalCommandBackend(config, state_dir)

            # Run and expect failure.
            with self.assertRaises(ModelUnavailable) as cm:
                backend.complete(prompt)
            self.assertIn("failed with exit", str(cm.exception))

            # Verify no prompt file left behind.
            prompt_dir = state_dir / "prompts"
            if prompt_dir.exists():
                remaining_files = list(prompt_dir.glob("prompt-*.txt"))
                self.assertEqual(
                    [],
                    remaining_files,
                    f"Prompt files left behind after failure: {remaining_files}",
                )

            token_found = self._scan_for_token(unique_token)
            self.assertEqual([], token_found, f"Token found in forbidden locations after failure: {token_found}")

    def test_config_defaults_use_stdin_to_true(self) -> None:
        """use_stdin must default to True when omitted from [model] configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                "[model]\n"
                'command = ["runner"]\n'
                "timeout_seconds = 5\n"
                "max_output_chars = 1000\n\n"
                "[conversation]\n"
                "history_turns = 2\n"
                "memory_results = 2\n\n"
                "[storage]\n"
                'state_dir = "data"\n',
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            self.assertTrue(cfg.model.use_stdin)

    def test_scanner_finds_planted_token_control(self) -> None:
        """Control test: scanner must find a token planted deep in temp without being cut off by depth/file limits."""
        token = f"CANARY_PLANTED_{uuid.uuid4().hex}"
        plant_base = Path(tempfile.mkdtemp(prefix="nova-plant-ctrl-"))
        try:
            # Create nested directory tree deep inside temp
            deep_dir = plant_base / "a" / "b" / "c" / "d" / "e" / "f"
            deep_dir.mkdir(parents=True, exist_ok=True)
            plant_file = deep_dir / "canary.bin"
            plant_file.write_bytes(os.urandom(1024) + token.encode("utf-8") + os.urandom(1024))
            found = self._scan_for_token(token)
            self.assertTrue(
                any(str(plant_file.resolve()) == str(Path(p).resolve()) for p in found),
                f"Planted token at {plant_file} was not found by scanner (found: {found})",
            )
        finally:
            shutil.rmtree(plant_base, ignore_errors=True)

    def test_bounded_output_does_not_hang_or_buffer_runaway_output(self) -> None:
        """A model runner producing runaway output must be bounded and not hang until timeout."""
        with tempfile.TemporaryDirectory() as state_dir_str:
            state_dir = Path(state_dir_str)
            runners_dir = state_dir / "runners"
            runners_dir.mkdir(parents=True, exist_ok=True)
            runner_script = runners_dir / "infinite_stream.py"
            # Runner outputs infinite stream of data
            runner_script.write_text(
                "import sys\n"
                "while True:\n"
                "    sys.stdout.write('X' * 4096)\n"
                "    sys.stdout.flush()\n",
                encoding="utf-8",
            )
            config = ModelConfig(
                command=(sys.executable, str(runner_script)),
                timeout_seconds=2,
                max_output_chars=500,
                use_stdin=True,
            )
            backend = LocalCommandBackend(config, state_dir)
            reply = backend.complete("test prompt")
            self.assertTrue(reply.startswith("X" * 500))
            self.assertIn("[response truncated]", reply)

    def test_full_companion_turn_with_stored_memory(self) -> None:
        """Store a memory with a token, run a full Companion turn, and verify no leak outside memory store."""
        with tempfile.TemporaryDirectory() as state_dir_str:
            state_dir = Path(state_dir_str)
            cwd_dir = state_dir / "cwd"
            cwd_dir.mkdir()
            token = f"CANARY_MEMORY_{uuid.uuid4().hex}"
            store_path = state_dir / "memories.jsonl"
            store = MemoryStore(store_path)
            store.add(f"the secret token is {token}")

            stub_script = self._write_stub_runner(state_dir, "companion_echo.py", success=True)
            config = ModelConfig(
                command=(sys.executable, str(stub_script)),
                timeout_seconds=5,
                max_output_chars=4000,
                use_stdin=True,
            )
            backend = LocalCommandBackend(config, state_dir)
            companion = Companion(backend, store, ConversationConfig(history_turns=2, memory_results=2))

            old_cwd = os.getcwd()
            os.chdir(cwd_dir)
            try:
                reply = companion.reply("what is the secret token?")
            finally:
                os.chdir(old_cwd)

            self.assertIn(token, reply)
            # Scan for leaks outside memories.jsonl
            found = self._scan_for_token(token, skip_paths={store_path.resolve()})
            self.assertEqual([], found, f"Token leaked outside memory store: {found}")

    @staticmethod
    def _write_stub_runner(
        state_dir: Path,
        script_name: str,
        success: bool = False,
        timeout: bool = False,
        fail: bool = False,
    ) -> Path:
        """Write a stub runner script to state_dir/runners/."""
        runners_dir = state_dir / "runners"
        runners_dir.mkdir(parents=True, exist_ok=True)
        script = runners_dir / script_name

        if timeout:
            code = "import time\ntime.sleep(100)"
        elif fail:
            code = "import sys\nsys.exit(1)"
        elif success:
            code = "import sys\ndata = sys.stdin.read()\nprint('echo:', data)"
        else:
            code = "print('ok')"

        script.write_text(code)
        return script

    @staticmethod
    def _scan_for_token(token: str, skip_paths: set[Path] | None = None) -> list[str]:
        """Scan system temp, current directory, and common locations for the token.

        Returns a list of paths where the token was found.
        """
        found = []
        skip = {p.resolve() for p in (skip_paths or set())}

        # Scan system temp directory.
        for temp_dir in [tempfile.gettempdir(), Path(tempfile.gettempdir())]:
            if Path(temp_dir).exists():
                for root, dirs, files in os.walk(temp_dir):
                    # Limit scan depth to avoid scanning the entire filesystem.
                    dirs[:] = dirs[:5]
                    for file_name in files[:20]:
                        file_path = Path(root) / file_name
                        if file_path.resolve() in skip:
                            continue
                        # Regular files only: opening a FIFO for reading blocks
                        # until a writer appears (CI run 38285668806 hung here).
                        if not file_path.is_file():
                            continue
                        try:
                            if file_path.read_bytes().find(token.encode()) >= 0:
                                found.append(str(file_path))
                        except Exception:
                            pass

        # Scan current directory (but not deeply).
        cwd = Path.cwd()
        for item in cwd.iterdir():
            if item.resolve() in skip:
                continue
            if item.is_file():
                try:
                    if item.read_bytes().find(token.encode()) >= 0:
                        found.append(str(item))
                except Exception:
                    pass

        return found

