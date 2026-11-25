"""Extra tests for novacore.cli and novacore.config: every slash command, error exits, the REPL loop."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from novacore import cli
from novacore.config import ConfigError, load_config

RUNNER = Path(__file__).with_name("fake_runner.py")
REPLY = "I am NovaCore, running strictly local via CLI."


def toml_path(p: object) -> str:
    return str(p).replace(chr(92), "/")


def config_text(command: str | None = None, extra_model: str = "", state_dir: str = '"st"') -> str:
    command = command or f'["{toml_path(sys.executable)}", "{toml_path(RUNNER)}"]'
    return (
        f"[model]\ncommand = {command}\ntimeout_seconds = 20\nmax_output_chars = 500\n{extra_model}\n"
        f"[conversation]\nhistory_turns = 3\nmemory_results = 3\n[storage]\nstate_dir = {state_dir}\n"
    )


class CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_test_cli_x_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = self.tmp / "c.toml"
        self.cfg.write_text(config_text(), encoding="utf-8")

    def run_cli(self, *args: str, stdin_lines: list[str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin_lines is None:
                rc = cli.main(["--config", str(self.cfg), *args])
            else:
                with mock.patch("builtins.input", side_effect=stdin_lines):
                    rc = cli.main(["--config", str(self.cfg)])
        return rc, out.getvalue(), err.getvalue()

    def msg(self, text: str) -> tuple[int, str, str]:
        return self.run_cli("--message", text)


class TestCommands(CliBase):
    def test_help_and_case_insensitive_command(self) -> None:
        rc, out, _ = self.msg("/HELP")
        self.assertEqual(rc, 0)
        self.assertIn("/remember TEXT", out)

    def test_plain_text_goes_to_the_model(self) -> None:
        self.assertEqual(self.msg("hello"), (0, REPL_OUT := REPLY + "\n", ""))

    def test_remember_then_mem_finds_it(self) -> None:
        rc, out, _ = self.msg("/remember buy milk")
        self.assertEqual(rc, 0)
        memory_id = out.split()[2].rstrip(".")
        rc, out, _ = self.msg("/mem milk")
        self.assertEqual(out.strip(), f"{memory_id}: buy milk")

    def test_remember_empty_and_mem_nothing(self) -> None:
        rc, _, err = self.msg("/remember   ")
        self.assertEqual(rc, 2)
        self.assertIn("cannot be empty", err)
        self.assertEqual(self.msg("/mem zzz")[1].strip(), "No matching saved memories.")

    def test_status_and_capabilities(self) -> None:
        self.assertIn("no physical hardware", self.msg("/status")[1])
        out = self.msg("/capabilities")[1]
        self.assertIn("system.status: available_now=True", out)
        self.assertIn("audio.speak: available_now=False", out)

    def test_request_usage_and_bad_json(self) -> None:
        for bad in ("/request", "/request audio.speak", "/confirm"):
            rc, _, err = self.msg(bad)
            self.assertEqual(rc, 2, bad)
            self.assertIn("usage: /", err)
        self.assertIn("must be JSON", self.msg("/request audio.speak nope")[2])
        self.assertIn("must be a JSON object", self.msg("/request audio.speak [1]")[2])

    def test_request_confirm_speak_report_unavailable(self) -> None:
        for line in ('/request audio.speak {}', '/confirm audio.speak {"text": "hi"}', "/speak hi"):
            rc, out, _ = self.msg(line)
            self.assertEqual(rc, 0, line)
            self.assertTrue(out.startswith("unavailable: audio.speak"), out)

    def test_confirm_differs_from_request_for_high_risk_capability(self) -> None:
        self.assertTrue(self.msg("/request camera.enable {}")[1].startswith("confirmation_required:"))
        confirmed = self.msg("/confirm camera.enable {}")[1]
        self.assertFalse(confirmed.startswith("confirmation_required:"), confirmed)
        self.assertTrue(confirmed.startswith("unavailable:"), confirmed)

    def test_propose_update(self) -> None:
        self.assertIn("usage: /propose-update", self.msg("/propose-update no separator")[2])
        rc, out, _ = self.msg("/propose-update Title | Summary text")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("proposed_unverified: wrote "))
        self.assertIn("no update was applied", out)
        self.assertEqual(len(list((self.tmp / "st" / "proposals").glob("*.json"))), 1)

    def test_unknown_command(self) -> None:
        rc, _, err = self.msg("/nonsense")
        self.assertEqual((rc, err.strip()), (2, "novacore: unknown command: /nonsense; use /help"))


class TestDemandCommand(CliBase):
    def test_empty_list_messages(self) -> None:
        self.assertEqual(self.msg("/demand")[1].strip(), "No open demands.")
        self.assertEqual(self.msg("/demand list all")[1].strip(), "No demands recorded.")

    def test_add_list_show_resolve_cycle(self) -> None:
        out = self.msg("/demand add Need tool | why | http://a, http://b | step one; step two")[1]
        demand_id = out.split()[3].rstrip(":")
        self.assertTrue(demand_id.startswith("demand-"), out)
        self.assertIn(f"{demand_id} [OPEN]: Need tool", self.msg("/demand list")[1])
        shown = self.msg(f"/demand show {demand_id}")[1]
        for needle in ("Need tool", "http://a", "http://b", "step one", "step two"):
            self.assertIn(needle, shown)
        self.assertIn("marked as fulfilled", self.msg(f"/demand resolve {demand_id}")[1])
        self.assertEqual(self.msg("/demand")[1].strip(), "No open demands.")
        self.assertIn(f"{demand_id} [", self.msg("/demand list all")[1])
        self.assertIn("Demand not found", self.msg(f"/demand resolve {demand_id}x")[1])

    def test_urls_are_split_on_commas_into_separate_entries(self) -> None:
        self.msg("/demand add T | d | http://a , http://b,, | s")
        from novacore.demand import DemandStore
        (entry,) = DemandStore(self.tmp / "st" / "demands.jsonl").list(status=None)
        self.assertEqual(list(entry.urls), ["http://a", "http://b"])

    def test_usage_errors(self) -> None:
        for bad, text in (
            ("/demand add a|b|c", "usage: /demand add"),
            ("/demand add only title", "usage: /demand add"),
            ("/demand show", "usage: /demand show"),
            ("/demand resolve", "usage: /demand resolve"),
            ("/demand export", "usage: /demand export"),
            ("/demand bogus", "unknown /demand subcommand: bogus"),
        ):
            rc, _, err = self.msg(bad)
            self.assertEqual(rc, 2, bad)
            self.assertIn(text, err)

    def test_show_missing_and_export(self) -> None:
        self.assertEqual(self.msg("/demand show nope")[1].strip(), "Demand not found: nope")
        target = self.tmp / "out"
        rc, out, _ = self.msg(f"/demand export {target}")
        self.assertEqual(rc, 0)
        self.assertIn("Exported 0 open demand(s)", out)
        self.assertTrue((target / "DEMAND.md").is_file())

    def test_export_into_a_file_is_a_clean_error_not_a_traceback(self) -> None:
        blocker = self.tmp / "afile"
        blocker.write_text("x", encoding="utf-8")
        rc, out, err = self.msg(f"/demand export {blocker}")
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("novacore: "), err)


class TestRecipeCommand(CliBase):
    def test_recipes_listing(self) -> None:
        for line in ("/recipes", "/recipe", "/recipe list"):
            out = self.msg(line)[1]
            self.assertTrue(out.startswith("Available recipes:"), line)
            self.assertIn("study_session", out)

    def test_unknown_recipe_and_bad_slots(self) -> None:
        self.assertIn("unknown recipe: nope", self.msg("/recipe nope")[2])
        self.assertIn("must be JSON", self.msg("/recipe study_session {bad")[2])
        self.assertIn("'subject' is required", self.msg("/recipe study_session {}")[2])

    def test_recipe_runs_with_slots(self) -> None:
        rc, out, _ = self.msg('/recipe study_session {"subject": "maths", "chapter": 3}')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("Recipe 'study_session'"))


class TestMainLoop(CliBase):
    def test_banner_and_quit(self) -> None:
        rc, out, _ = self.run_cli(stdin_lines=["/quit"])
        self.assertEqual(rc, 0)
        self.assertIn("Type /help", out)

    def test_eof_and_ctrl_c_exit_cleanly(self) -> None:
        for exc in (EOFError, KeyboardInterrupt):
            rc, _, _ = self.run_cli(stdin_lines=[exc()])
            self.assertEqual(rc, 0)

    def test_blank_lines_skipped_errors_do_not_end_the_session(self) -> None:
        rc, out, _ = self.run_cli(stdin_lines=["", "/nonsense", "hello", "/EXIT"])
        self.assertEqual(rc, 0)
        self.assertIn("novacore: unknown command: /nonsense", out)
        self.assertIn(REPLY, out)

    def test_os_error_in_a_command_does_not_kill_the_session(self) -> None:
        blocker = self.tmp / "afile"
        blocker.write_text("x", encoding="utf-8")
        rc, out, _ = self.run_cli(stdin_lines=[f"/demand export {blocker}", "hello", "/quit"])
        self.assertEqual(rc, 0)
        self.assertIn(REPLY, out)


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_test_cfg_x_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, text: str):
        p = self.tmp / "c.toml"
        p.write_text(text, encoding="utf-8")
        return load_config(p)

    def test_valid_config_resolves_state_dir_next_to_the_file(self) -> None:
        cfg = self.load(config_text())
        self.assertEqual(cfg.state_dir, (self.tmp / "st").resolve())
        self.assertEqual((cfg.model.timeout_seconds, cfg.model.max_output_chars, cfg.model.use_stdin), (20, 500, True))
        self.assertEqual((cfg.conversation.history_turns, cfg.conversation.memory_results), (3, 3))

    def test_missing_file_and_bad_toml(self) -> None:
        with self.assertRaisesRegex(ConfigError, "not found"):
            load_config(self.tmp / "none.toml")
        with self.assertRaisesRegex(ConfigError, "invalid TOML"):
            self.load("[model\n")

    def test_missing_tables(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"\[model\] must be a TOML table"):
            self.load("x = 1\n")

    def test_bad_commands(self) -> None:
        for command in ("[]", '"a string"', '["ok", ""]', "[1]"):
            with self.assertRaises(ConfigError, msg=command):
                self.load(config_text(command=command))

    def test_use_stdin_rules(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be a boolean"):
            self.load(config_text(extra_model='use_stdin = "yes"'))
        with self.assertRaisesRegex(ConfigError, "cannot contain"):
            self.load(config_text(command='["m", "{prompt_file}"]', extra_model="use_stdin = true"))
        with self.assertRaisesRegex(ConfigError, "must contain a literal"):
            self.load(config_text(extra_model="use_stdin = false"))
        cfg = self.load(config_text(command='["m", "{prompt_file}"]', extra_model="use_stdin = false"))
        self.assertFalse(cfg.model.use_stdin)

    def test_state_dir_must_be_a_non_empty_string(self) -> None:
        for bad in ('""', "5"):
            with self.assertRaisesRegex(ConfigError, "state_dir"):
                self.load(config_text(state_dir=bad))

    def test_positive_ints_reject_zero_bool_and_text(self) -> None:
        for bad in ("0", "-1", "true", '"5"', "1.5"):
            text = config_text().replace("timeout_seconds = 20", f"timeout_seconds = {bad}")
            with self.assertRaisesRegex(ConfigError, "positive integer", msg=bad):
                self.load(text)


class TestMainConfigErrors(unittest.TestCase):
    def test_config_error_exits_2_with_message(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(Path(tempfile.gettempdir()) / "nova-no-such-config.toml")])
        self.assertEqual(rc, 2)
        self.assertIn("configuration error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
