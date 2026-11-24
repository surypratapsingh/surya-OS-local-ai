"""Unit tests for tools/check-citations.py (stdlib unittest only).

Each test builds a throwaway repo tree in a temp directory and points the
tool's module globals at it, so the real docs/logs/aud-citations.log is never
written. The expected outcomes come from the file layout the test creates.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "check-citations.py"))

_spec = importlib.util.spec_from_file_location("check_citations", TOOL)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


class FakeRepo:
    """A temp repo root wired into the tool's module globals."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="cc-test-"))
        self.saved = {k: getattr(cc, k) for k in
                      ("REPO_ROOT", "DOCS_DIR", "README", "PLAN", "OUTPUT_LOG")}
        cc.REPO_ROOT = self.root
        cc.DOCS_DIR = self.root / "docs"
        cc.README = self.root / "README.md"
        cc.PLAN = self.root / "plan.md"
        cc.OUTPUT_LOG = self.root / "docs" / "logs" / "aud-citations.log"
        (self.root / "docs" / "logs").mkdir(parents=True)

    def write(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf8")
        return p

    def close(self):
        for k, v in self.saved.items():
            setattr(cc, k, v)
        shutil.rmtree(self.root, ignore_errors=True)


class ValidCitationTests(unittest.TestCase):
    def test_path_with_directory_is_valid(self):
        self.assertTrue(cc.is_valid_path_citation("docs/logs/a.log"))
        self.assertTrue(cc.is_valid_path_citation(r"docs\logs\a.log"))

    def test_bare_extension_is_rejected(self):
        # Spec: bare extensions are not paths and must not be checked.
        for bare in ("md", "py", "rs", "log", "sh", "cfg", "toml"):
            self.assertFalse(cc.is_valid_path_citation(bare), bare)

    def test_known_single_word_directory_is_valid(self):
        self.assertTrue(cc.is_valid_path_citation("kernel"))

    def test_plain_sentence_is_rejected(self):
        self.assertFalse(cc.is_valid_path_citation("two words"))


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()

    def tearDown(self):
        self.repo.close()

    def test_docs_citation_is_extracted(self):
        self.repo.write("docs/notes.md", "see docs/logs/a.log for the run\n")
        self.assertIn("docs/logs/a.log", cc.extract_paths_from_docs())

    def test_backticked_plan_citation_is_extracted(self):
        self.repo.write("plan.md", "evidence: `tools/keygen.py` ok\n")
        self.assertIn("tools/keygen.py", cc.extract_paths_from_plan())

    def test_readme_citation_is_extracted(self):
        self.repo.write("README.md", "uses tools/verify-disk.py to check\n")
        self.assertIn("tools/verify-disk.py", cc.extract_paths_from_readme())

    def test_citation_text_without_directory_is_not_extracted(self):
        self.repo.write("README.md", "the file is called keygen.py\n")
        self.assertEqual(cc.extract_paths_from_readme(), set())


class ExistenceTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()

    def tearDown(self):
        self.repo.close()

    def test_existing_file_resolves(self):
        self.repo.write("tools/present-check.py", "")
        ok, resolved = cc.check_path_exists("tools/present-check.py")
        self.assertTrue(ok)
        self.assertTrue(resolved.endswith("present-check.py"))

    def test_missing_file_does_not_resolve(self):
        ok, resolved = cc.check_path_exists("docs/zz-missing-7f3a.log")
        self.assertFalse(ok)
        self.assertIsNone(resolved)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()

    def tearDown(self):
        self.repo.close()

    def run_main(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cc.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def test_all_citations_present_exits_0(self):
        self.repo.write("tools/real-tool.py", "")
        self.repo.write("README.md", "uses tools/real-tool.py\n")
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        log = cc.OUTPUT_LOG.read_text(encoding="utf8")
        self.assertIn("Missing: 0", log)
        self.assertIn("[OK]   tools/real-tool.py", log)

    def test_missing_citation_exits_1_and_is_logged(self):
        self.repo.write("docs/notes.md", "uses docs/zz-missing-7f3a.log here\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("MISSING PATHS", out)
        log = cc.OUTPUT_LOG.read_text(encoding="utf8")
        self.assertIn("[FAIL] docs/zz-missing-7f3a.log", log)
        self.assertIn("Missing: 1", log)


if __name__ == "__main__":
    unittest.main()
