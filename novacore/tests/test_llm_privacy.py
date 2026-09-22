"""Test that owner content does not leak to disk outside NOVA state directory."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from novacore.config import ModelConfig
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
    def _scan_for_token(token: str) -> list[str]:
        """Scan system temp, current directory, and common locations for the token.

        Returns a list of paths where the token was found.
        """
        found = []

        # Scan system temp directory.
        for temp_dir in [tempfile.gettempdir(), Path(tempfile.gettempdir())]:
            if Path(temp_dir).exists():
                for root, dirs, files in os.walk(temp_dir):
                    # Limit scan depth to avoid scanning the entire filesystem.
                    dirs[:] = dirs[:5]
                    for file_name in files[:20]:
                        file_path = Path(root) / file_name
                        try:
                            if file_path.read_bytes().find(token.encode()) >= 0:
                                found.append(str(file_path))
                        except Exception:
                            pass

        # Scan current directory (but not deeply).
        cwd = Path.cwd()
        for item in cwd.iterdir():
            if item.is_file():
                try:
                    if item.read_bytes().find(token.encode()) >= 0:
                        found.append(str(item))
                except Exception:
                    pass

        return found
