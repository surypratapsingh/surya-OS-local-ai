"""
K6-7 / A2.28: Linux Process Start Stack Oracle & hello-musl Verification.

Validates:
1. The kernel/user/hello-musl crate adheres to project invariants (std-only, no crates, prints fixed line and argv).
2. The System V AMD64 Linux initial process stack layout oracle (gABI / Linux x86_64 ABI):
   - rsp % 16 == 0 at process entry point
   - [rsp] == argc
   - [rsp + 8 ..] == argv pointers, terminated by NULL
   - [rsp + 8*(argc+2) ..] == envp pointers, terminated by NULL
   - auxv pairs (tag, val) containing AT_PHDR, AT_PHNUM, AT_PAGESZ=4096, AT_ENTRY, AT_RANDOM
   - terminated by AT_NULL (tag 0, val 0)
"""

import os
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Linux x86_64 Auxiliary Vector Tags
AT_NULL = 0
AT_IGNORE = 1
AT_EXECFD = 2
AT_PHDR = 3
AT_PHENT = 4
AT_PHNUM = 5
AT_PAGESZ = 6
AT_BASE = 7
AT_FLAGS = 8
AT_ENTRY = 9
AT_NOTELF = 10
AT_UID = 11
AT_EUID = 12
AT_GID = 13
AT_EGID = 14
AT_CLKTCK = 17
AT_RANDOM = 25
AT_EXECFN = 31

USER_STACK_PAGE = 0x007F_F000
USER_STACK_TOP = 0x0080_0000


def build_linux_stack_oracle(argv: list[str], envp: list[str], auxv_dict: dict[int, int]) -> tuple[int, bytes]:
    """
    Independent Python oracle simulating kernel/src/proc.rs::setup_linux_stack.
    Returns (user_rsp, stack_page_4096_bytes).
    """
    buf = bytearray(4096)
    cur_offset = 4096 - 16

    # 1. 16 random bytes at the top
    random_bytes = b"\xd4\x1d\x8c\xd9\x8f\x00\xb2\x04\xe9\x80\x09\x98\xec\xf8\x42\x7e"
    buf[cur_offset : cur_offset + 16] = random_bytes
    at_random_vaddr = USER_STACK_TOP - 16

    # 2. env strings
    env_vaddrs = []
    for s in envp:
        encoded = s.encode("utf-8") + b"\0"
        cur_offset -= len(encoded)
        buf[cur_offset : cur_offset + len(encoded)] = encoded
        env_vaddrs.append(USER_STACK_PAGE + cur_offset)

    # 3. argv strings
    argv_vaddrs = []
    for s in argv:
        encoded = s.encode("utf-8") + b"\0"
        cur_offset -= len(encoded)
        buf[cur_offset : cur_offset + len(encoded)] = encoded
        argv_vaddrs.append(USER_STACK_PAGE + cur_offset)

    # 4. align cur_offset to 8 bytes
    cur_offset &= ~7

    # 5. build table words
    words = []
    # argc
    words.append(len(argv))
    # argv pointers
    for va in argv_vaddrs:
        words.append(va)
    # NULL
    words.append(0)
    # envp pointers
    for va in env_vaddrs:
        words.append(va)
    # NULL
    words.append(0)

    # Auxv entries
    execfn_vaddr = argv_vaddrs[0] if argv_vaddrs else 0
    auxv_pairs = [
        (AT_PHDR, auxv_dict.get(AT_PHDR, 0)),
        (AT_PHENT, auxv_dict.get(AT_PHENT, 56)),
        (AT_PHNUM, auxv_dict.get(AT_PHNUM, 0)),
        (AT_PAGESZ, auxv_dict.get(AT_PAGESZ, 4096)),
        (AT_BASE, 0),
        (AT_FLAGS, 0),
        (AT_ENTRY, auxv_dict.get(AT_ENTRY, 0x0040_0000)),
        (AT_UID, 1000),
        (AT_EUID, 1000),
        (AT_GID, 1000),
        (AT_EGID, 1000),
        (AT_CLKTCK, 100),
        (AT_RANDOM, at_random_vaddr),
        (AT_EXECFN, execfn_vaddr),
        (AT_NULL, 0),
    ]
    for tag, val in auxv_pairs:
        words.append(tag)
        words.append(val)

    total_bytes = len(words) * 8
    table_start_offset = (cur_offset - total_bytes) & ~15
    assert table_start_offset >= 128, "stack overflow"

    for i, w in enumerate(words):
        struct.pack_into("<Q", buf, table_start_offset + i * 8, w)

    user_rsp = USER_STACK_PAGE + table_start_offset
    return user_rsp, bytes(buf)


def validate_linux_stack(rsp: int, page_bytes: bytes) -> dict:
    """
    Validate that a given stack page complies with System V AMD64 Linux ABI requirements.
    """
    errors = []
    if rsp % 16 != 0:
        errors.append(f"rsp not 16-byte aligned: {rsp:#x}")
    if rsp < USER_STACK_PAGE or rsp >= USER_STACK_TOP:
        errors.append(f"rsp out of bounds: {rsp:#x}")

    table_off = rsp - USER_STACK_PAGE
    argc = struct.unpack_from("<Q", page_bytes, table_off)[0]
    idx = 1
    argv = []
    while True:
        off = table_off + idx * 8
        if off >= 4096:
            errors.append("argv table overflowed page")
            break
        ptr = struct.unpack_from("<Q", page_bytes, off)[0]
        idx += 1
        if ptr == 0:
            break
        # read null-terminated string
        str_off = ptr - USER_STACK_PAGE
        if 0 <= str_off < 4096:
            null_pos = page_bytes.find(b"\0", str_off)
            argv.append(page_bytes[str_off:null_pos].decode("utf-8", "replace"))
        else:
            errors.append(f"argv ptr out of bounds: {ptr:#x}")

    if len(argv) != argc:
        errors.append(f"argc mismatch: header says {argc}, found {len(argv)}")

    envp = []
    while True:
        off = table_off + idx * 8
        if off >= 4096:
            errors.append("envp table overflowed page")
            break
        ptr = struct.unpack_from("<Q", page_bytes, off)[0]
        idx += 1
        if ptr == 0:
            break
        str_off = ptr - USER_STACK_PAGE
        if 0 <= str_off < 4096:
            null_pos = page_bytes.find(b"\0", str_off)
            envp.append(page_bytes[str_off:null_pos].decode("utf-8", "replace"))
        else:
            errors.append(f"envp ptr out of bounds: {ptr:#x}")

    auxv = {}
    while True:
        off = table_off + idx * 8
        if off + 16 > 4096:
            errors.append("auxv table overflowed page without AT_NULL")
            break
        tag = struct.unpack_from("<Q", page_bytes, off)[0]
        val = struct.unpack_from("<Q", page_bytes, off + 8)[0]
        idx += 2
        if tag == AT_NULL:
            break
        auxv[tag] = val

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "argc": argc,
        "argv": argv,
        "envp": envp,
        "auxv": auxv,
    }


class TestHelloMuslCrate(unittest.TestCase):
    def test_hello_musl_cargo_toml(self):
        cargo_path = ROOT / "kernel" / "user" / "hello-musl" / "Cargo.toml"
        self.assertTrue(cargo_path.exists(), "Cargo.toml must exist")
        content = cargo_path.read_text(encoding="utf-8")
        self.assertIn('name = "hello-musl"', content)
        self.assertIn("[dependencies]", content)
        # Verify no external crates added
        lines = [line.strip() for line in content.splitlines()]
        dep_idx = lines.index("[dependencies]")
        deps = [l for l in lines[dep_idx + 1 :] if l and not l.startswith("#") and not l.startswith("[")]
        self.assertEqual(len(deps), 0, f"No dependencies allowed: {deps}")

    def test_hello_musl_source(self):
        main_path = ROOT / "kernel" / "user" / "hello-musl" / "src" / "main.rs"
        self.assertTrue(main_path.exists(), "src/main.rs must exist")
        content = main_path.read_text(encoding="utf-8")
        self.assertIn("hello from nucleus linux abi", content)
        self.assertIn("argv[", content)


class TestLinuxStackOracle(unittest.TestCase):
    def test_oracle_valid_stack(self):
        argv = ["hello-musl", "test1", "arg2"]
        envp = ["PORT=8080", "USER=nucleus"]
        auxv = {
            AT_PHDR: 0x0040_0040,
            AT_PHNUM: 4,
            AT_PAGESZ: 4096,
            AT_ENTRY: 0x0040_0000,
        }
        rsp, page = build_linux_stack_oracle(argv, envp, auxv)
        res = validate_linux_stack(rsp, page)
        self.assertTrue(res["valid"], f"Validation failed: {res['errors']}")
        self.assertEqual(res["argc"], 3)
        self.assertEqual(res["argv"], argv)
        self.assertEqual(res["envp"], envp)
        self.assertEqual(res["auxv"][AT_PHDR], 0x0040_0040)
        self.assertEqual(res["auxv"][AT_PHNUM], 4)
        self.assertEqual(res["auxv"][AT_PAGESZ], 4096)
        self.assertEqual(res["auxv"][AT_ENTRY], 0x0040_0000)
        self.assertEqual(res["auxv"][AT_RANDOM], USER_STACK_TOP - 16)

    def test_oracle_catches_misaligned_rsp(self):
        argv = ["hello-musl"]
        envp = []
        auxv = {AT_PAGESZ: 4096}
        rsp, page = build_linux_stack_oracle(argv, envp, auxv)
        # Sabotage: misalign rsp by +8
        res = validate_linux_stack(rsp + 8, page)
        self.assertFalse(res["valid"])
        self.assertTrue(any("not 16-byte aligned" in e for e in res["errors"]))

    def test_oracle_catches_corrupted_argc(self):
        argv = ["hello-musl", "foo"]
        envp = []
        auxv = {AT_PAGESZ: 4096}
        rsp, page = build_linux_stack_oracle(argv, envp, auxv)
        # Sabotage: modify argc byte
        page_mod = bytearray(page)
        off = rsp - USER_STACK_PAGE
        struct.pack_into("<Q", page_mod, off, 99)
        res = validate_linux_stack(rsp, bytes(page_mod))
        self.assertFalse(res["valid"])
        self.assertTrue(any("argc mismatch" in e for e in res["errors"]))

    def test_oracle_catches_missing_at_null(self):
        argv = ["hello-musl"]
        envp = []
        auxv = {AT_PAGESZ: 4096}
        rsp, page = build_linux_stack_oracle(argv, envp, auxv)
        # Sabotage: overwrite AT_NULL with non-zero tag
        page_mod = bytearray(page)
        # Find 0, 0 terminator in auxv and change tag to 99
        off = rsp - USER_STACK_PAGE
        # argc (1) + argv (1) + null (1) + null (1) = 4 words
        # 14 pairs = 28 words
        at_null_off = off + (4 + 28) * 8
        struct.pack_into("<Q", page_mod, at_null_off, 99)
        res = validate_linux_stack(rsp, bytes(page_mod))
        self.assertFalse(res["valid"])
        self.assertTrue(any("without AT_NULL" in e for e in res["errors"]))


if __name__ == "__main__":
    unittest.main()
