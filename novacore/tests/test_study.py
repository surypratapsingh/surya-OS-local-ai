"""Unit tests for NOVA Study Session (work order A3.9, milestone M2).

Verifies:
1. Question file parsing (JSON array and JSONL).
2. Missing mathd-cli binary behavior: appends demand entry via DemandStore and NEVER guesses verdict.
3. RAM privacy (W5): answers stay in memory or owner's CaseMemory; no disk leakage.
4. Real mathd-cli integration: tests use the real binary when built, and SKIP loudly when not.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from novacore.casememory import CaseMemory
from novacore.demand import DemandStore
from novacore.study import (
    EvaluationResult,
    Question,
    StudySession,
    find_mathd_cli,
    load_questions,
)


class TestStudySession(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nova_test_study_"))
        self.demand_path = self.temp_dir / "demands.jsonl"
        self.demand_store = DemandStore(self.demand_path)

        # Create a sample question file
        self.sample_questions = [
            Question(
                id="q1",
                prompt="Expand (x + 1)^2",
                claim_template="identity: (x + 1)^2 == {answer}",
                category="algebra",
            ),
            Question(
                id="q2",
                prompt="Evaluate the indefinite integral of 2*x with respect to x",
                claim_template="integral: 2*x -> {answer}",
                category="calculus",
            ),
        ]
        self.q_file = self.temp_dir / "questions.json"
        self.q_file.write_text(
            json.dumps([
                {
                    "id": "q1",
                    "prompt": "Expand (x + 1)^2",
                    "claim_template": "identity: (x + 1)^2 == {answer}",
                    "category": "algebra",
                },
                {
                    "id": "q2",
                    "prompt": "Evaluate the indefinite integral of 2*x with respect to x",
                    "claim_template": "integral: 2*x -> {answer}",
                    "category": "calculus",
                },
            ]),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_questions_from_file(self) -> None:
        """Questions load accurately from JSON and JSONL files."""
        loaded = load_questions(self.q_file)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].id, "q1")
        self.assertEqual(loaded[1].category, "calculus")

        # Test JSONL format
        jsonl_file = self.temp_dir / "questions.jsonl"
        jsonl_file.write_text(
            '{"id": "q1", "prompt": "p1", "claim_template": "identity: {answer}"}\n'
            '{"id": "q2", "prompt": "p2", "claim_template": "identity: {answer}"}\n',
            encoding="utf-8",
        )
        loaded_jsonl = load_questions(jsonl_file)
        self.assertEqual(len(loaded_jsonl), 2)

    def test_missing_mathd_cli_appends_demand_and_never_guesses_verdict(self) -> None:
        """When mathd-cli is missing, a demand entry is logged and verdict is NEVER guessed."""
        # Explicitly pass a nonexistent path for mathd_cli
        session = StudySession(
            question_source=self.sample_questions,
            mathd_cli=Path("/nonexistent/binary/mathd-cli"),
            demand_store=self.demand_store,
        )

        res = session.submit_answer("q1", "x^2 + 2*x + 1")

        # Invariant: verdict is NOT guessed as Verified or Failed
        self.assertEqual(res.verdict, "DemandLogged")
        self.assertIsNone(res.evidence)
        self.assertIsNotNone(res.demand_entry_id)

        # Check demand store
        demands = self.demand_store.all()
        self.assertEqual(len(demands), 1)
        d = demands[0]
        self.assertEqual(d.entry_id, res.demand_entry_id)
        self.assertEqual(d.item_type, "binary")
        self.assertEqual(d.target_filename, "mathd-cli")
        self.assertIn("q1", d.description)
        self.assertGreater(len(d.instructions), 0)

    def test_answers_stay_in_ram_w5_privacy(self) -> None:
        """Answers stay in RAM; no answer files written to disk."""
        session = StudySession(
            question_source=self.sample_questions,
            mathd_cli=Path("/nonexistent/binary/mathd-cli"),
            demand_store=self.demand_store,
        )

        session.submit_answer("q1", "student_secret_ram_answer_12345")

        self.assertEqual(len(session.results), 1)
        self.assertEqual(session.results[0].student_answer, "student_secret_ram_answer_12345")

        # Verify no file anywhere in temp_dir (including demands.jsonl) contains the student answer
        self.assertTrue(self.demand_path.exists())
        for root, _, files in os.walk(self.temp_dir):
            for file_name in files:
                f_path = Path(root) / file_name
                content = f_path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn(
                    "student_secret_ram_answer_12345",
                    content,
                    f"Student answer leaked to disk in {f_path}!",
                )

    def test_missing_points_or_error_marks_unverifiable_without_inventing_values(self) -> None:
        """If points_tested or max_relative_error cannot be parsed, verdict is Unverifiable (never 16/0.0)."""
        if os.name == "nt":
            fake_bin = Path(self.temp_dir) / "fake_mathd.cmd"
            fake_bin.write_text("@echo off\necho Verdict: Verified\necho unparseable output\n", encoding="utf-8")
        else:
            fake_bin = Path(self.temp_dir) / "fake_mathd.sh"
            fake_bin.write_text("#!/bin/sh\necho 'Verdict: Verified'\necho 'unparseable output'\n", encoding="utf-8")
            fake_bin.chmod(0o755)

        session = StudySession(
            question_source=self.sample_questions,
            mathd_cli=fake_bin,
            demand_store=self.demand_store,
        )

        res = session.submit_answer("q1", "x^2 + 2*x + 1")

        # Must NOT invent points_tested=16 and max_relative_error=0.0 with Verified verdict
        self.assertEqual(res.verdict, "Unverifiable")
        self.assertIsNotNone(res.evidence)
        self.assertEqual(res.evidence.get("verdict"), "Unverifiable")
        self.assertIn("Missing points_tested", res.evidence.get("reason", ""))

    def test_real_mathd_cli_integration_or_skip_loudly(self) -> None:
        """Tests use the real mathd-cli binary when built, and SKIP loudly when not."""
        real_bin = find_mathd_cli()
        if real_bin is None:
            self.skipTest(
                "SKIP LOUDLY: mathd-cli binary is not built on this host; "
                "skipping live mathd-cli verification tests per A3.9 requirement."
            )

        # If mathd-cli is present, run real mathematical evaluation
        session = StudySession(
            question_source=self.sample_questions,
            mathd_cli=real_bin,
            demand_store=self.demand_store,
        )

        # 1. Correct answer -> Verified
        res_correct = session.submit_answer("q1", "x^2 + 2*x + 1")
        self.assertEqual(res_correct.verdict, "Verified")
        self.assertIsNotNone(res_correct.evidence)
        self.assertGreaterEqual(res_correct.evidence["points_tested"], 1)

        # 2. Incorrect answer -> Failed
        res_wrong = session.submit_answer("q1", "x^2 + 99*x + 1")
        self.assertEqual(res_wrong.verdict, "Failed")

        summary = session.get_summary()
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["demand_logged"], 0)


if __name__ == "__main__":
    unittest.main()
