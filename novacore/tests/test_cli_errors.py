"""An I/O failure inside a slash command must not kill the CLI session.

Found by running the CLI headless: `/demand export <unwritable path>` (a pendrive
that is not mounted, a read-only disk) raised a raw OSError traceback, and in the
interactive loop that ended the whole session.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from novacore import cli

CONFIG = """
[model]
command = ["cat"]
timeout_seconds = 5
max_output_chars = 2000
[conversation]
history_turns = 2
memory_results = 2
[storage]
state_dir = "state"
"""


class CliIoErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "nova.toml").write_text(CONFIG, encoding="utf-8")
        self.config = root / "nova.toml"
        # A regular file where a directory is needed: mkdir raises NotADirectoryError.
        (root / "afile").write_text("x", encoding="utf-8")
        self.bad_target = root / "afile" / "sub"

    def run_cli(self, argv: list[str], stdin_lines: list[str] | None = None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if stdin_lines is None:
                code = cli.main(argv)
            else:
                with patch("builtins.input", side_effect=stdin_lines):
                    code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_message_mode_reports_io_error_with_exit_2(self) -> None:
        code, _, err = self.run_cli(
            ["--config", str(self.config), "--message", f"/demand export {self.bad_target}"]
        )
        self.assertEqual(2, code)
        self.assertIn("novacore:", err)

    def test_interactive_session_survives_io_error(self) -> None:
        code, out, _ = self.run_cli(
            ["--config", str(self.config)],
            [f"/demand export {self.bad_target}", "/help", "/quit"],
        )
        self.assertEqual(0, code)
        self.assertIn("novacore:", out)
        self.assertIn("/remember TEXT", out)  # the /help after the failure still ran


if __name__ == "__main__":
    unittest.main()
