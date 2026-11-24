"""Unit tests for tools/atomic-pointer-swap.py (stdlib unittest only).

Known defect (found 2026-11-17, not fixed here: tool is outside this lane):
struct_unpack_pointer() unpacks with format '<I B 3s I I 32s I' (52 bytes)
from a 64-byte buffer, so read_pointer_block() always raises
"unpack requires a buffer of 52 bytes". The pack side writes 48 bytes and
the CRC_OFFSET constant is 56, so the on-disk layout is also inconsistent.
Tests that need a readable block are marked expectedFailure and say why.
When the defect is fixed, the expectedFailure marks turn into
"unexpected success" and must be removed.

Oracles: the CRC-32 check value from the CRC catalogue (ISO-HDLC,
'123456789' -> 0xCBF43926), and the field widths written in the tool's
struct format string.
"""

import importlib.util
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "atomic-pointer-swap.py"))

_spec = importlib.util.spec_from_file_location("atomic_pointer_swap", TOOL)
aps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aps)

MAGIC = 0x4E4F5641  # "NOVA" as written by the tool


def make_fields(active=1, seq=5, ts=1700000000, manifest="nova.b"):
    return dict(magic=MAGIC, active_slot=active, sequence=seq,
                timestamp=ts, manifest_path=manifest)


def block_with_crc(fields, crc=None):
    body = aps.struct_pack_pointer(fields["magic"], fields["active_slot"],
                                   fields["sequence"], fields["timestamp"],
                                   fields["manifest_path"])
    if crc is None:
        crc = zlib.crc32(body) & 0xFFFFFFFF
    blk = body + struct.pack("<I", crc)
    return blk + b"\x00" * (aps.POINTER_SIZE - len(blk))


class Crc32Tests(unittest.TestCase):
    def test_check_value_matches_catalogue(self):
        self.assertEqual(aps.crc32(b"123456789"), 0xCBF43926)

    def test_empty_input(self):
        self.assertEqual(aps.crc32(b""), 0)


class PackTests(unittest.TestCase):
    def test_packed_fields_are_48_bytes(self):
        # Field widths from the format '<I B 3s I I 32s': 4+1+3+4+4+32.
        body = aps.struct_pack_pointer(MAGIC, 1, 5, 1700000000, "nova.b")
        self.assertEqual(len(body), 48)

    def test_magic_is_first_and_little_endian(self):
        body = aps.struct_pack_pointer(MAGIC, 0, 1, 0, "x")
        self.assertEqual(body[:4], b"\x41\x56\x4F\x4E")  # "AVON" as LE bytes
        self.assertEqual(body[4], 0)

    def test_manifest_path_is_padded_to_32_bytes(self):
        body = aps.struct_pack_pointer(MAGIC, 0, 1, 0, "nova.b")
        self.assertEqual(body[-32:], b"nova.b" + b"\x00" * 26)


class ValidateTests(unittest.TestCase):
    def pb(self, **kw):
        f = make_fields(**kw)
        f["crc32"] = zlib.crc32(aps.struct_pack_pointer(
            f["magic"], f["active_slot"], f["sequence"],
            f["timestamp"], f["manifest_path"])) & 0xFFFFFFFF
        return f

    def test_accepts_block_with_matching_crc(self):
        self.assertTrue(aps.validate_pointer_block(self.pb()))

    def test_rejects_wrong_crc(self):
        f = self.pb()
        f["crc32"] ^= 1
        self.assertFalse(aps.validate_pointer_block(f))

    def test_rejects_slot_two(self):
        self.assertFalse(aps.validate_pointer_block(self.pb(active=2)))

    def test_rejects_bad_magic(self):
        f = self.pb()
        f["magic"] = 0xDEADBEEF
        self.assertFalse(aps.validate_pointer_block(f))

    def test_rejects_bad_magic_even_when_crc_matches_it(self):
        # CRC is computed over the bad-magic fields, so only the magic
        # check can reject this block.
        f = make_fields()
        f["magic"] = 0xDEADBEEF
        f["crc32"] = zlib.crc32(aps.struct_pack_pointer(
            f["magic"], f["active_slot"], f["sequence"],
            f["timestamp"], f["manifest_path"])) & 0xFFFFFFFF
        self.assertFalse(aps.validate_pointer_block(f))


class SwapArgumentTests(unittest.TestCase):
    """These raise before any file is read, so they do not hit the defect.

    The message is matched, not just the type: the tool's print of a
    non-ASCII arrow raises UnicodeEncodeError (a ValueError subclass) on
    Windows consoles with a cp1252 code page, which would pass a type-only
    check for the wrong reason.
    """

    def test_invalid_new_slot_raises(self):
        with self.assertRaisesRegex(ValueError, "Invalid slot: 2"):
            aps.atomic_pointer_swap(Path("/nonexistent"), 0, 2, 2, "m")

    def test_zero_sequence_raises(self):
        with self.assertRaisesRegex(ValueError, "Invalid sequence: 0"):
            aps.atomic_pointer_swap(Path("/nonexistent"), 0, 1, 0, "m")


class ReadBackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aps-test-")

    def write_block(self, blk):
        p = Path(self.dir) / "ptr.bin"
        p.write_bytes(blk)
        return p

    @unittest.expectedFailure
    def test_read_pointer_block_round_trips_fields(self):
        # Known defect: unpack needs 52 bytes; this raises struct.error.
        p = self.write_block(block_with_crc(make_fields(active=1, seq=5)))
        pb = aps.read_pointer_block(p)
        self.assertEqual(pb["active_slot"], 1)
        self.assertEqual(pb["sequence"], 5)
        self.assertEqual(pb["manifest_path"], "nova.b")

    @unittest.expectedFailure
    def test_read_then_validate_accepts_a_good_block(self):
        # Known defect, same cause as above.
        p = self.write_block(block_with_crc(make_fields()))
        self.assertTrue(aps.validate_pointer_block(aps.read_pointer_block(p)))


if __name__ == "__main__":
    unittest.main()
