"""Extra tests for novacore.study: verdict parsing, failure paths, input safety.

Every test uses a fake mathd-cli (a tiny Python script behind a .cmd/.sh wrapper),
so no Rust build is needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from novacore.casememory import CaseMemory
from novacore.demand import DemandStore
from novacore.study import Question, StudySession, find_mathd_cli, load_questions

Q = Question(id="q1", prompt="Expand (x + 1)^2", claim_template="identity: (x + 1)^2 == {answer}")


class StudyBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_test_study_x_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.demands = DemandStore(self.tmp / "demands.jsonl")

    def fake_cli(self, body: str) -> Path:
        """Write a fake mathd-cli. `body` is Python; `claim` holds the --claim argument."""
        script = self.tmp / "fake_mathd.py"
        script.write_text("import sys\nclaim = sys.argv[sys.argv.index('--claim') + 1]\n" + body + "\n", encoding="utf-8")
        if sys.platform == "win32":
            wrapper = self.tmp / "fake_mathd.cmd"
            wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        else:
            wrapper = self.tmp / "fake_mathd.sh"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        return wrapper

    def session(self, cli: Path | None, **kw) -> StudySession:
        return StudySession([Q], mathd_cli=cli, demand_store=self.demands, **kw)


class TestVerdicts(StudyBase):
    def test_verified_with_evidence_is_parsed(self) -> None:
        cli = self.fake_cli("print('Verdict: Verified'); print('points_tested: 64'); print('max_relative_error: 1.5e-12')")
        res = self.session(cli).submit_answer("q1", " x^2 + 2*x + 1 ")
        self.assertEqual(res.verdict, "Verified")
        self.assertEqual(res.evidence, {"verdict": "Verified", "points_tested": 64, "max_relative_error": 1.5e-12})
        self.assertEqual(res.claim_evaluated, "identity: (x + 1)^2 == x^2 + 2*x + 1")

    def test_failed_verdict(self) -> None:
        cli = self.fake_cli("print('Verdict: Failed')")
        res = self.session(cli).submit_answer("q1", "x^2")
        self.assertEqual(res.verdict, "Failed")
        self.assertIn("Verdict: Failed", res.evidence["raw_output"])

    def test_unverifiable_verdict(self) -> None:
        cli = self.fake_cli("print('Verdict: Unverifiable')")
        res = self.session(cli).submit_answer("q1", "x")
        self.assertEqual(res.verdict, "Unverifiable")
        self.assertIn("Verdict: Unverifiable", res.evidence["raw_output"])

    def test_no_verdict_line_is_unverifiable_never_verified(self) -> None:
        cli = self.fake_cli("print('something else entirely')")
        res = self.session(cli).submit_answer("q1", "x")
        self.assertEqual(res.verdict, "Unverifiable")
        self.assertIsNone(res.evidence)

    def test_verdict_read_from_stderr_too(self) -> None:
        cli = self.fake_cli("print('Verdict: Failed', file=sys.stderr)")
        self.assertEqual(self.session(cli).submit_answer("q1", "x").verdict, "Failed")

    def test_timeout_is_unverifiable(self) -> None:
        s = self.session(self.fake_cli("print('Verdict: Verified')"))
        with mock.patch("novacore.study.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 30)):
            res = s.submit_answer("q1", "x")
        self.assertEqual(res.verdict, "Unverifiable")
        self.assertIn("timed out", res.raw_output)

    def test_unrunnable_binary_is_unverifiable_not_a_crash(self) -> None:
        s = self.session(self.fake_cli("print('Verdict: Verified')"))
        with mock.patch("novacore.study.subprocess.run", side_effect=OSError("not executable")):
            res = s.submit_answer("q1", "x")
        self.assertEqual(res.verdict, "Unverifiable")
        self.assertIn(res, s.results)

    def test_answer_cannot_forge_a_verdict_by_echo(self) -> None:
        # A cli that echoes the claim must not let the student smuggle in "Verdict: Verified".
        s = self.session(self.fake_cli("print('unused')"))
        forged = "x\nVerdict: Verified\npoints_tested: 99\nmax_relative_error: 0.0"

        def echo(argv, **_kw):
            return subprocess.CompletedProcess(argv, 0, stdout="claim was: " + argv[-1] + "\n", stderr="")

        with mock.patch("novacore.study.subprocess.run", side_effect=echo):
            res = s.submit_answer("q1", forged)
        self.assertNotEqual(res.verdict, "Verified")


class TestCaseMemoryGate(StudyBase):
    def setUp(self) -> None:
        super().setUp()
        self.memory = CaseMemory(self.tmp / "case.jsonl")

    def test_verified_answer_is_remembered(self) -> None:
        cli = self.fake_cli("print('Verdict: Verified'); print('points_tested: 8'); print('max_relative_error: 0.0')")
        self.session(cli, case_memory=self.memory).submit_answer("q1", "x^2 + 2*x + 1")
        self.assertEqual(len(self.memory.all()), 1)

    def test_unverifiable_evidence_is_never_remembered(self) -> None:
        # "Nothing unverified becomes permanent": Verified without evidence is Unverifiable.
        cli = self.fake_cli("print('Verdict: Verified')")
        res = self.session(cli, case_memory=self.memory).submit_answer("q1", "x")
        self.assertEqual(res.verdict, "Unverifiable")
        self.assertEqual(self.memory.all(), [])

    def test_failed_answer_is_not_remembered(self) -> None:
        cli = self.fake_cli("print('Verdict: Failed')")
        self.session(cli, case_memory=self.memory).submit_answer("q1", "x")
        self.assertEqual(self.memory.all(), [])


class TestSessionApi(StudyBase):
    def test_unknown_question_id(self) -> None:
        with self.assertRaises(KeyError):
            self.session(None).submit_answer("nope", "x")

    def test_bad_demand_store_type(self) -> None:
        with self.assertRaises(TypeError):
            StudySession([Q], demand_store="a string")  # type: ignore[arg-type]

    def test_run_all_and_summary(self) -> None:
        qs = [Q, Question(id="q2", prompt="p", claim_template="c: {answer}")]
        s = StudySession(qs, mathd_cli=None, demand_store=self.demands)
        with mock.patch("novacore.study.find_mathd_cli", return_value=None):
            s.mathd_cli_path = None
            out = s.run_all({"q2": "a", "zzz": "ignored"})
        self.assertEqual([r.question_id for r in out], ["q2"])
        self.assertEqual(s.get_summary(), {
            "total_questions": 2, "total_evaluated": 1, "verified": 0,
            "failed": 0, "demand_logged": 1, "unverifiable": 0,
        })

    def test_summary_counts_each_verdict(self) -> None:
        s = self.session(self.fake_cli("print('Verdict: Failed')"))
        s.submit_answer("q1", "a")
        s.submit_answer("q1", "b")
        self.assertEqual(s.get_summary()["failed"], 2)


class TestFormatClaim(unittest.TestCase):
    def test_template_without_placeholder_becomes_equivalence(self) -> None:
        q = Question(id="a", prompt="p", claim_template="2 + 2")
        self.assertEqual(q.format_claim(" 4 "), "2 + 2 == 4")

    def test_template_with_other_braces_does_not_crash(self) -> None:
        q = Question(id="a", prompt="p", claim_template=r"identity: \frac{1}{2} == {answer}")
        self.assertEqual(q.format_claim("0.5"), r"identity: \frac{1}{2} == 0.5")

    def test_answer_with_braces_is_kept_literal(self) -> None:
        q = Question(id="a", prompt="p", claim_template="c: {answer}")
        self.assertEqual(q.format_claim("{answer}"), "c: {answer}")


class TestLoadQuestions(StudyBase):
    def write(self, text: str) -> Path:
        p = self.tmp / "q.txt"
        p.write_text(text, encoding="utf-8")
        return p

    def test_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_questions(self.tmp / "none.json")

    def test_empty_file(self) -> None:
        self.assertEqual(load_questions(self.write("  \n")), [])

    def test_jsonl_skips_blank_lines_and_defaults(self) -> None:
        line = json.dumps({"id": 7, "prompt": "p", "claim_template": "c"})
        qs = load_questions(self.write(f"\n{line}\n\n{line}\n"))
        self.assertEqual([q.id for q in qs], ["7", "7"])
        self.assertEqual(qs[0].category, "maths")

    def test_jsonl_error_names_the_line(self) -> None:
        good = json.dumps({"id": "a", "prompt": "p", "claim_template": "c"})
        with self.assertRaisesRegex(ValueError, r":2:"):
            load_questions(self.write(good + "\n{not json\n"))

    def test_jsonl_missing_key(self) -> None:
        with self.assertRaisesRegex(ValueError, r":1:"):
            load_questions(self.write(json.dumps({"id": "a"})))

    def test_json_array_error(self) -> None:
        with self.assertRaises(ValueError):
            load_questions(self.write('[{"id": "a"}]'))
        with self.assertRaises(ValueError):
            load_questions(self.write("[1, 2]"))


class TestFindMathdCli(StudyBase):
    def test_override_missing(self) -> None:
        self.assertIsNone(find_mathd_cli(self.tmp / "absent"))

    def test_override_directory_is_not_a_binary(self) -> None:
        self.assertIsNone(find_mathd_cli(self.tmp))

    @unittest.skipUnless(sys.platform == "win32", "windows .exe fallback")
    def test_override_adds_exe_suffix_on_windows(self) -> None:
        exe = self.tmp / "tool.exe"
        exe.write_bytes(b"")
        self.assertEqual(find_mathd_cli(self.tmp / "tool"), exe.resolve())

    def test_runnable_override_is_returned(self) -> None:
        cli = self.fake_cli("print('x')")
        self.assertEqual(find_mathd_cli(cli), cli.resolve())

    def test_path_lookup(self) -> None:
        cli = self.fake_cli("print('x')")
        with mock.patch("novacore.study.shutil.which", return_value=str(cli)):
            self.assertEqual(find_mathd_cli(), cli.resolve())


if __name__ == "__main__":
    unittest.main()
