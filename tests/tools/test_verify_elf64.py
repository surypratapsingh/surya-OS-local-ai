"""Unit tests for tools/verify-elf64.py (stdlib unittest only).

Images are built here from the ELF64 layout in the gABI (ch4 ELF header,
ch5 program header, 64-byte Ehdr and 56-byte Phdr), not with the tool's own
structs. Expected verdicts and staged hashes come from the test's own
construction. Each test breaks one rule and checks the tool rejects it.
"""

import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "verify-elf64.py"))

EHDR = "<16sHHIQQQIHHHHHH"  # gABI Ehdr, 64 bytes
PHDR = "<IIQQQQQQ"          # gABI Phdr, 56 bytes
PT_LOAD = 1
PT_INTERP = 3
ET_EXEC = 2
ET_DYN = 3
PAGE = 0x1000
WINDOW = 0x8000000
SLOT = 0x200000

IDENT_OK = b"\x7fELF" + bytes([2, 1, 1]) + b"\x00" * 9


def build_elf(payload, entry=None, e_type=ET_EXEC, phentsize=56,
              magic=b"\x7fELF", memsz=None, interp=False):
    phnum = 2 if interp else 1
    phoff = 64
    payload_off = phoff + 56 * phnum
    # Segment at file offset payload_off, vaddr congruent to offset mod PAGE.
    p_vaddr = 0x401000 + payload_off
    filesz = len(payload)
    if memsz is None:
        memsz = filesz + 0x100
    if entry is None:
        entry = p_vaddr
    ident = magic.ljust(4, b"\x00")[:4] + bytes([2, 1, 1]) + b"\x00" * 9
    ehdr = struct.pack(EHDR, ident, e_type, 62, 1, entry, phoff, 0, 0,
                       64, phentsize, phnum, 64, 0, 0)
    load = struct.pack(PHDR, PT_LOAD, 5, payload_off, p_vaddr, p_vaddr,
                       filesz, memsz, PAGE)
    phdrs = load
    if interp:
        phdrs += struct.pack(PHDR, PT_INTERP, 4, 0, 0, 0, 0, 0, 1)
    return ehdr + phdrs + payload, p_vaddr


def build_two_loads(p1, p2):
    """Two PT_LOAD segments; the entry sits in the first. Offsets follow the
    gABI rule p_vaddr % PAGE == p_offset % PAGE for each segment."""
    phoff = 64
    off1 = phoff + 56 * 2
    off2 = off1 + len(p1)
    v1 = 0x401000 + off1
    v2 = 0x600000 + off2
    ehdr = struct.pack(EHDR, IDENT_OK, ET_EXEC, 62, 1, v1, phoff, 0, 0,
                       64, 56, 2, 64, 0, 0)
    ph = struct.pack(PHDR, PT_LOAD, 5, off1, v1, v1, len(p1), len(p1), PAGE)
    ph += struct.pack(PHDR, PT_LOAD, 6, off2, v2, v2, len(p2), len(p2), PAGE)
    return ehdr + ph + p1 + p2


class VerifyElf64Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="elf64-test-")
        self.payload = bytes(range(100))

    def run_tool(self, data):
        path = os.path.join(self.dir, "img.elf")
        with open(path, "wb") as f:
            f.write(data)
        p = subprocess.run(
            [sys.executable, TOOL, path],
            capture_output=True, text=True,
        )
        return p.returncode, p.stdout

    def test_valid_image_is_accepted(self):
        data, vaddr = build_elf(self.payload)
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 0, out)
        self.assertIn("ELFORACLE VERDICT valid", out)
        self.assertIn(f"ELFORACLE seg 0 base {WINDOW:#x} delta {vaddr % PAGE:#x}", out)

    def test_staged_hash_is_over_segment_bytes_plus_zero_fill(self):
        # Independent expected value: the payload followed by 0x100 zero bytes
        # (memsz - filesz), hashed with hashlib. The test builds this itself.
        data, _ = build_elf(self.payload)
        want = hashlib.sha256(self.payload + b"\x00" * 0x100).hexdigest()
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"ELFORACLE staged_sha256 {want}", out)
        self.assertIn(f"ELFORACLE staged_len {len(self.payload) + 0x100}", out)

    def test_bad_magic_rejected(self):
        data, _ = build_elf(self.payload, magic=b"XELF")
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 1)
        self.assertIn("bad magic", out)

    def test_pie_rejected_by_et_exec_policy(self):
        data, _ = build_elf(self.payload, e_type=ET_DYN)
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 1)
        self.assertIn("ET_EXEC", out)

    def test_entry_outside_every_load_rejected(self):
        data, _ = build_elf(self.payload, entry=0x1000)
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 1)
        self.assertIn("outside every PT_LOAD", out)

    def test_interp_rejected(self):
        data, _ = build_elf(self.payload, interp=True)
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 1)
        self.assertIn("PT_INTERP", out)

    def test_memsz_smaller_than_filesz_rejected(self):
        data, _ = build_elf(self.payload, memsz=len(self.payload) - 1)
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 1)
        self.assertIn("p_memsz < p_filesz", out)

    def test_wrong_phentsize_rejected(self):
        data, _ = build_elf(self.payload, phentsize=48)
        rc, out = self.run_tool(data)
        self.assertEqual(rc, 1)
        self.assertIn("e_phentsize", out)


class TwoSegmentTests(unittest.TestCase):
    def test_second_segment_stages_one_slot_up(self):
        p1 = bytes(range(64))
        p2 = bytes(range(200, 232))
        data = build_two_loads(p1, p2)
        path = os.path.join(tempfile.mkdtemp(prefix="elf64-two-"), "two.elf")
        with open(path, "wb") as f:
            f.write(data)
        p = subprocess.run([sys.executable, TOOL, path],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        # Window + slot per segment index.
        self.assertIn(f"ELFORACLE seg 0 base {WINDOW:#x}", p.stdout)
        self.assertIn(f"ELFORACLE seg 1 base {WINDOW + SLOT:#x}", p.stdout)
        want = hashlib.sha256(p1 + p2).hexdigest()
        self.assertIn(f"ELFORACLE staged_sha256 {want}", p.stdout)


if __name__ == "__main__":
    unittest.main()
