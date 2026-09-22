from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from novacore.config import ConfigError, ModelConfig, load_config
from novacore.llm import LocalCommandBackend, ModelUnavailable


class ConfigAndLlmTests(unittest.TestCase):
    def test_config_requires_prompt_file_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(
                "[model]\n"
                'command = ["runner"]\n'
                "timeout_seconds = 1\n"
                "max_output_chars = 10\n"
                "use_stdin = false\n\n"
                "[conversation]\n"
                "history_turns = 1\n"
                "memory_results = 1\n\n"
                "[storage]\n"
                'state_dir = "data"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(config)

    def test_runner_receives_prompt_file_without_a_shell(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            backend = LocalCommandBackend(
                ModelConfig(
                    command=(sys.executable, "-c", "print('local reply')", "{prompt_file}"),
                    timeout_seconds=5,
                    max_output_chars=100,
                    use_stdin=False,
                ),
                Path(state_dir),
            )
            self.assertEqual("local reply", backend.complete("private prompt"))

    def test_missing_runner_fails_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            backend = LocalCommandBackend(
                ModelConfig(
                    command=("definitely-not-a-novacore-runner", "{prompt_file}"),
                    timeout_seconds=1,
                    max_output_chars=100,
                    use_stdin=False,
                ),
                Path(state_dir),
            )
            with self.assertRaises(ModelUnavailable):
                backend.complete("hello")
