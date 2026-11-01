"""N5: the document writers, judged by programs that share no code with them.

Oracles: Poppler's pdftotext and pdfinfo read the PDF; pandoc and LibreOffice
read the DOCX. A test whose tool is missing is skipped and says so; CI
installs poppler-utils and pandoc (.github/workflows/check.yml).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from novacore import documents

TITLE = "Physics (notes) \\ ch.3"
TEXT = "Newton's laws: F = ma (second law). Café, résumé & <tags>. नमस्ते"


def run(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, check=True, timeout=180).stdout.decode("utf-8", "replace")


class FolderTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def test_nothing_is_ever_overwritten(self) -> None:
        first = documents.create(self.folder, "Notes", "txt", "one")
        second = documents.create(self.folder, "Notes", "txt", "two")
        self.assertEqual(("Notes.txt", "Notes (2).txt"), (first.name, second.name))
        self.assertEqual("Notes\n\none\n", first.read_text(encoding="utf-8"))

    def test_names_are_safe(self) -> None:
        self.assertEqual("etc passwd", documents.file_name("../../etc/passwd"))
        self.assertEqual("NOVA com1", documents.file_name("com1"))  # reserved in any case; the owner's case is kept
        self.assertEqual("Untitled", documents.file_name("???"))
        self.assertEqual(60, len(documents.file_name("x" * 200)))

    def test_text_file(self) -> None:
        path = documents.create(self.folder, "Groceries", "txt", "milk eggs and bread")
        self.assertEqual("Groceries\n\nmilk eggs and bread\n", path.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("pdftotext") and shutil.which("pdfinfo"), "SKIPPED: Poppler (pdftotext, pdfinfo) not installed")
class PdfTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def test_poppler_reads_back_the_title_and_text(self) -> None:
        path = documents.create(self.folder, TITLE, "pdf", TEXT)
        self.assertIn("Pages:           1", run("pdfinfo", str(path)))
        text = run("pdftotext", str(path), "-")
        self.assertIn(TITLE, text)
        # Outside WinAnsi, Devanagari is written as "?" (documented in documents.py).
        self.assertIn("Newton's laws: F = ma (second law). Café, résumé & <tags>. ??????", text)

    def test_unbalanced_parentheses_and_backslashes_survive(self) -> None:
        # Added after plant P1 (parentheses unescaped) passed: balanced pairs
        # need no escape (ISO 32000-1, 7.3.4.2), so only unbalanced ones test it.
        text = "Q1) first :( second \\ third ((("
        path = documents.create(self.folder, "Q1) quiz :(", "pdf", text)
        out = run("pdftotext", str(path), "-")
        self.assertIn("Q1) quiz :(", out)
        self.assertIn(text, out)

    def test_long_text_spans_pages_and_loses_no_word(self) -> None:
        words = [f"word{i}" for i in range(2500)]
        path = documents.create(self.folder, "long", "pdf", " ".join(words))
        pages = int(run("pdfinfo", str(path)).split("Pages:")[1].split()[0])
        self.assertGreater(pages, 1)
        found = run("pdftotext", str(path), "-").replace("\f", " ").split()
        self.assertEqual(words, [w for w in found if w.startswith("word")])


@unittest.skipUnless(shutil.which("pandoc"), "SKIPPED: pandoc not installed")
class DocxPandocTests(unittest.TestCase):
    def test_pandoc_reads_back_the_title_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = documents.create(Path(directory), TITLE, "docx", TEXT)
            text = run("pandoc", "-t", "plain", "--wrap=none", str(path))
        self.assertIn(TITLE, text)
        self.assertIn(TEXT, text)


@unittest.skipUnless(shutil.which("soffice") and sys.platform.startswith("linux"), "SKIPPED: LibreOffice not installed")
class DocxLibreOfficeTests(unittest.TestCase):
    def test_libreoffice_reads_back_the_title_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = documents.create(Path(directory), TITLE, "docx", TEXT)
            run("soffice", f"-env:UserInstallation=file://{directory}/profile", "--headless",
                "--convert-to", "txt:Text", "--outdir", directory, str(path))
            text = (Path(directory) / (path.stem + ".txt")).read_text(encoding="utf-8-sig")
        self.assertIn(TITLE, text)
        self.assertIn(TEXT, text)


if __name__ == "__main__":
    unittest.main()
