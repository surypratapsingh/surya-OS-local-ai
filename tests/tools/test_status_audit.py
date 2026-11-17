"""Unit tests for tools/status-audit.py helper functions (stdlib unittest only).

main() writes the committed audit log, so it is not run here. The helpers
are tested in a temp repo whose REPO_ROOT and LOGS_DIR are set by the test.

Known defect (found 2026-11-17, not fixed here: tool is outside this lane):
find_log_file() cannot resolve wildcard references such as
'docs/logs/k3a-*.log' (cited in plan.md). The wildcard branch compares
'docs/logs/k3a-.log' with bare file names, which never contains a slash, so
it never matches. The wildcard test is marked expectedFailure and must be
unmarked once the tool is fixed.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "status-audit.py"))

_spec = importlib.util.spec_from_file_location("status_audit", TOOL)
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)


class TempRepo:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="sa-test-"))
        self.logs = self.root / "docs" / "logs"
        self.logs.mkdir(parents=True)
        self.saved = sa.REPO_ROOT

    def write_log(self, name, text=""):
        p = self.logs / name
        p.write_text(text, encoding="utf8")
        return p

    def close(self):
        sa.REPO_ROOT = self.saved
        shutil.rmtree(self.root, ignore_errors=True)


class FindLogFileTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        sa.REPO_ROOT = self.repo.root

    def tearDown(self):
        self.repo.close()

    def test_full_repo_path_is_found(self):
        self.repo.write_log("ci-111-mathd.log", "x")
        got = sa.find_log_file("docs/logs/ci-111-mathd.log", self.repo.logs)
        self.assertIsNotNone(got)
        self.assertTrue(got.endswith("ci-111-mathd.log"))

    def test_short_name_is_found_in_logs_dir(self):
        self.repo.write_log("r14-d2-zero-sockets.log", "x")
        got = sa.find_log_file("r14-d2-zero-sockets.log", self.repo.logs)
        self.assertIsNotNone(got)
        self.assertTrue(got.endswith("r14-d2-zero-sockets.log"))

    def test_missing_log_returns_none(self):
        self.assertIsNone(sa.find_log_file("docs/logs/zz-absent-7f3a.log", self.repo.logs))

    @unittest.expectedFailure
    def test_wildcard_reference_finds_matching_log(self):
        # Known defect: 'k3a-*.log' should match 'k3a-check-full.log'.
        self.repo.write_log("k3a-check-full.log", "x")
        got = sa.find_log_file("docs/logs/k3a-*.log", self.repo.logs)
        self.assertIsNotNone(got)


class GrepLogPatternTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()

    def tearDown(self):
        self.repo.close()

    def test_returns_first_matching_line_number_and_text(self):
        p = self.repo.write_log("g.log", "alpha\nbeta PASS here\nPASS again\n")
        n, line = sa.grep_log_pattern(p, r"PASS")
        self.assertEqual(n, 2)
        self.assertEqual(line, "beta PASS here")

    def test_match_is_case_insensitive(self):
        p = self.repo.write_log("g.log", "one\nall Checks passed\n")
        n, line = sa.grep_log_pattern(p, r"checks PASSED")
        self.assertEqual(n, 2)

    def test_no_match_returns_none_pair(self):
        p = self.repo.write_log("g.log", "nothing here\n")
        self.assertEqual(sa.grep_log_pattern(p, r"SKIP"), (None, None))

    def test_missing_file_returns_none_pair(self):
        self.assertEqual(
            sa.grep_log_pattern(self.repo.logs / "absent.log", r"x"), (None, None))


class ExtractRefsTests(unittest.TestCase):
    def test_backtick_reference_is_extracted(self):
        # Only the backtick rule can match this name: no docs/logs prefix,
        # no ci- prefix, and no work-order pattern.
        refs = sa.extract_log_refs_from_text("evidence `gate-check-result.log` ok")
        self.assertIn("gate-check-result.log", refs)

    def test_bare_docs_logs_path_is_extracted(self):
        refs = sa.extract_log_refs_from_text("see docs/logs/k2-exception-gate.log for it")
        self.assertIn("docs/logs/k2-exception-gate.log", refs)

    def test_ci_run_name_is_extracted(self):
        refs = sa.extract_log_refs_from_text("CI run ci-37609824617-mathd passed")
        self.assertIn("ci-37609824617-mathd", refs)

    def test_plain_prose_gives_no_refs(self):
        self.assertEqual(sa.extract_log_refs_from_text("nothing cited here"), set())


if __name__ == "__main__":
    unittest.main()
