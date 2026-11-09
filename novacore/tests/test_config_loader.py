"""load_config validation: every rejected shape raises ConfigError and a good file loads."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from novacore.config import ConfigError, load_config

GOOD = """
[model]
command = ["runner", "--flag"]
timeout_seconds = 30
max_output_chars = 4000

[conversation]
history_turns = 4
memory_results = 3

[storage]
state_dir = "state"
"""


class LoadConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def load(self, text: str):
        path = self.dir / "nova.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_good_file_loads_with_stdin_default_and_resolved_state_dir(self) -> None:
        config = self.load(GOOD)
        self.assertEqual(("runner", "--flag"), config.model.command)
        self.assertTrue(config.model.use_stdin)
        self.assertEqual((self.dir / "state").resolve(), config.state_dir)
        self.assertEqual((4, 3), (config.conversation.history_turns, config.conversation.memory_results))

    def test_missing_file_and_bad_toml(self) -> None:
        with self.assertRaisesRegex(ConfigError, "not found"):
            load_config(self.dir / "absent.toml")
        with self.assertRaisesRegex(ConfigError, "invalid TOML"):
            self.load("[model\n")

    def test_missing_tables(self) -> None:
        for table in ("model", "conversation", "storage"):
            text = GOOD.replace(f"[{table}]", f"[x{table}]")
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, "must be a TOML table"):
                self.load(text)

    def test_bad_command(self) -> None:
        for bad in ('[]', '"runner"', '["a", ""]', '["a", 1]'):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.load(GOOD.replace('["runner", "--flag"]', bad))

    def test_positive_int_fields_reject_zero_negative_bool_and_string(self) -> None:
        for key, line in (
            ("timeout_seconds", "timeout_seconds = 30"),
            ("history_turns", "history_turns = 4"),
            ("memory_results", "memory_results = 3"),
            ("max_output_chars", "max_output_chars = 4000"),
        ):
            for bad in ("0", "-1", "true", '"5"'):
                with self.subTest(key=key, bad=bad), self.assertRaisesRegex(ConfigError, key):
                    self.load(GOOD.replace(line, f"{key} = {bad}"))

    def test_use_stdin_must_be_bool_and_match_prompt_file_placeholder(self) -> None:
        with self.assertRaisesRegex(ConfigError, "boolean"):
            self.load(GOOD.replace("[conversation]", 'use_stdin = "yes"\n[conversation]'))
        with self.assertRaisesRegex(ConfigError, "cannot contain"):
            self.load(GOOD.replace('"--flag"', '"{prompt_file}"'))
        with self.assertRaisesRegex(ConfigError, "must contain a literal"):
            self.load(GOOD.replace("[conversation]", "use_stdin = false\n[conversation]"))
        ok = self.load(GOOD.replace('"--flag"', '"{prompt_file}"').replace("[conversation]", "use_stdin = false\n[conversation]"))
        self.assertFalse(ok.model.use_stdin)

    def test_state_dir_required(self) -> None:
        for bad in ('""', "5"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ConfigError, "state_dir"):
                self.load(GOOD.replace('"state"', bad))
        with self.assertRaisesRegex(ConfigError, "state_dir"):
            self.load(GOOD.replace('state_dir = "state"', ""))


if __name__ == "__main__":
    unittest.main()
