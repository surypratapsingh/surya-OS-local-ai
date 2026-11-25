"""Extra tests for novacore.llm: runner failures, output bounds, env scrubbing, prompt-file cleanup.

Each test runs a tiny Python script as the "model runner"; nothing needs a real model.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from novacore.config import ModelConfig
from novacore.llm import LocalCommandBackend, ModelUnavailable


class LlmBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_test_llm_x_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def backend(self, code: str, *, use_stdin: bool = True, max_chars: int = 200, timeout: int = 20,
                extra_args: tuple[str, ...] = ()) -> LocalCommandBackend:
        script = self.tmp / "runner.py"
        script.write_text(code, encoding="utf-8")
        command = (sys.executable, str(script), *extra_args)
        return LocalCommandBackend(ModelConfig(command, timeout, max_chars, use_stdin), self.tmp / "state")


class TestRunner(LlmBase):
    def test_stdin_prompt_reaches_runner_and_reply_is_stripped(self) -> None:
        b = self.backend("import sys; sys.stdout.write('  echo:' + sys.stdin.read() + '\\n\\n')")
        self.assertEqual(b.complete("héllo"), "echo:héllo")

    def test_nonzero_exit_reports_code_and_stderr_tail(self) -> None:
        b = self.backend("import sys; sys.stderr.write('boom detail'); sys.exit(3)")
        with self.assertRaisesRegex(ModelUnavailable, r"exit 3: boom detail"):
            b.complete("p")

    def test_nonzero_exit_without_stderr_says_so(self) -> None:
        with self.assertRaisesRegex(ModelUnavailable, "no diagnostic output"):
            self.backend("import sys; sys.exit(1)").complete("p")

    def test_empty_output_is_unavailable_not_an_empty_reply(self) -> None:
        with self.assertRaisesRegex(ModelUnavailable, "returned no text"):
            self.backend("print('   ')").complete("p")

    def test_missing_runner(self) -> None:
        b = LocalCommandBackend(ModelConfig((str(self.tmp / "nope-runner"),), 5, 100, True), self.tmp)
        with self.assertRaisesRegex(ModelUnavailable, "not found"):
            b.complete("p")

    def test_runner_that_cannot_start_is_unavailable_not_a_crash(self) -> None:
        # A file that is not executable raises PermissionError / OSError, not FileNotFoundError.
        junk = self.tmp / "not_a_program"
        junk.write_text("plain text", encoding="utf-8")
        b = LocalCommandBackend(ModelConfig((str(junk),), 5, 100, True), self.tmp)
        with self.assertRaises(ModelUnavailable):
            b.complete("p")

    def test_timeout_kills_a_runner_that_never_answers(self) -> None:
        b = self.backend("import time; time.sleep(30)", timeout=1)
        with self.assertRaisesRegex(ModelUnavailable, "timed out after 1 seconds"):
            b.complete("p")

    def test_timeout_when_runner_prints_then_hangs(self) -> None:
        b = self.backend("import sys, time; print('partial'); sys.stdout.flush(); time.sleep(30)", timeout=1)
        with self.assertRaisesRegex(ModelUnavailable, "timed out"):
            b.complete("p")


class TestOutputBound(LlmBase):
    def test_long_output_is_cut_at_max_with_marker(self) -> None:
        b = self.backend("print('a' * 5000)", max_chars=100)
        reply = b.complete("p")
        self.assertEqual(reply, "a" * 100 + "\n[response truncated]")

    def test_runaway_runner_is_stopped(self) -> None:
        b = self.backend("import sys\nwhile True: sys.stdout.write('x' * 4096); sys.stdout.flush()", max_chars=50, timeout=20)
        self.assertEqual(b.complete("p"), "x" * 50 + "\n[response truncated]")

    def test_output_exactly_at_the_limit_is_not_marked_truncated(self) -> None:
        b = self.backend("print('b' * 100)", max_chars=100)
        self.assertEqual(b.complete("p"), "b" * 100)

    def test_short_output_has_no_marker(self) -> None:
        self.assertEqual(self.backend("print('ok')", max_chars=100).complete("p"), "ok")

    def test_multibyte_output_is_decoded_not_mangled(self) -> None:
        b = self.backend("import sys; sys.stdout.buffer.write(('é' * 3000).encode('utf-8'))", max_chars=2000)
        self.assertEqual(b.complete("p"), "é" * 2000 + "\n[response truncated]")


class TestEnvironment(LlmBase):
    CODE = "import os, sys; sys.stdout.write(','.join(sorted(os.environ)))"

    def test_secrets_and_proxies_are_not_passed_on(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "HTTP_PROXY": "http://p", "HTTPS_PROXY": "http://p",
               "API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "s", "GITHUB_TOKEN": "t", "ALL_PROXY": "http://p"}
        with mock.patch.dict(os.environ, env, clear=False):
            seen = set(self.backend(self.CODE).complete("p").split(","))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
            self.assertNotIn(name, seen)

    def test_only_documented_variables_survive(self) -> None:
        env = LocalCommandBackend._scrubbed_env()
        allowed = {"PATH", "HOME", "USER", "LANG", "LC_ALL"}
        if os.name == "nt":
            allowed |= {"USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR"}
        self.assertLessEqual(set(env), allowed)
        self.assertIn("PATH", env)

    def test_scrubbed_env_keeps_values_and_skips_unset(self) -> None:
        with mock.patch.dict(os.environ, {"LANG": "en_XX.UTF-8"}, clear=False):
            os.environ.pop("LC_ALL", None)
            env = LocalCommandBackend._scrubbed_env()
        self.assertEqual(env["LANG"], "en_XX.UTF-8")
        self.assertNotIn("LC_ALL", env)


class TestPromptFile(LlmBase):
    def prompt_dir(self) -> Path:
        return self.tmp / "state" / "prompts"

    def test_file_mode_substitutes_placeholder_and_deletes_the_file(self) -> None:
        code = "import sys; sys.stdout.write(open(sys.argv[1], encoding='utf-8').read())"
        b = self.backend(code, use_stdin=False, extra_args=("{prompt_file}",))
        self.assertEqual(b.complete("secret prompt text"), "secret prompt text")
        self.assertEqual(list(self.prompt_dir().iterdir()), [])

    def test_file_is_deleted_even_when_runner_fails(self) -> None:
        b = self.backend("import sys; sys.exit(2)", use_stdin=False, extra_args=("{prompt_file}",))
        with self.assertRaises(ModelUnavailable):
            b.complete("p")
        self.assertEqual(list(self.prompt_dir().iterdir()), [])

    def test_file_is_deleted_even_on_timeout(self) -> None:
        b = self.backend("import time; time.sleep(30)", use_stdin=False, timeout=1, extra_args=("{prompt_file}",))
        with self.assertRaises(ModelUnavailable):
            b.complete("p")
        self.assertEqual(list(self.prompt_dir().iterdir()), [])

    def test_stdin_mode_writes_no_prompt_file(self) -> None:
        self.backend("print('ok')").complete("private")
        self.assertFalse(self.prompt_dir().exists())

    def test_exclusive_create_failure_is_unavailable(self) -> None:
        b = self.backend("print('ok')", use_stdin=False, extra_args=("{prompt_file}",))
        with mock.patch("novacore.llm.os.open", side_effect=FileExistsError("taken")):
            with self.assertRaisesRegex(ModelUnavailable, "failed to create prompt file"):
                b.complete("p")

    def test_prompt_file_is_created_exclusively_with_mode_0600(self) -> None:
        b = self.backend("print('ok')", use_stdin=False, extra_args=("{prompt_file}",))
        real_open = os.open
        calls = []

        def spy(path, flags, mode=0o777, **kw):
            calls.append((flags, mode))
            return real_open(path, flags, mode, **kw)

        with mock.patch("novacore.llm.os.open", side_effect=spy):
            b.complete("p")
        flags, mode = calls[0]
        self.assertTrue(flags & os.O_EXCL and flags & os.O_CREAT)
        self.assertEqual(mode, 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_prompt_file_is_owner_only(self) -> None:
        code = "import os, sys; sys.stdout.write(oct(os.stat(sys.argv[1]).st_mode & 0o777))"
        b = self.backend(code, use_stdin=False, extra_args=("{prompt_file}",))
        self.assertEqual(b.complete("p"), "0o600")

    def test_unlink_failure_warns_but_reply_still_returned(self) -> None:
        b = self.backend("print('ok')", use_stdin=False, extra_args=("{prompt_file}",))
        with mock.patch.object(Path, "unlink", side_effect=OSError("locked")), \
                mock.patch("novacore.llm.time.sleep"), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(b.complete("p"), "ok")
        written = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("warning: failed to unlink prompt file", written)


if __name__ == "__main__":
    unittest.main()
