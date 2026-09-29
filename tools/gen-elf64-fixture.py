#!/usr/bin/env python3
"""Generate the K4b ELF64 loader fixture and its negative variants.

Writes:
    kernel/fixtures/elf64-mini            the positive fixture
    kernel/fixtures/negative/<name>.elf   one negative per defect class

The layout is FIXED and documented below; kernel/src/elfselftest.rs carries
hand golden tables derived from this documented layout plus the generic ABI
(https://refspecs.linuxbase.org/elf/gabi4+/ch4.eheader.html and ch5.pheader.html),
and tools/verify-elf64.py re-parses the emitted bytes independently. Nothing
in the repo derives expectations by running the loader under test.

Documented layout (all multi-byte fields little-endian, per gABI "Data
Encodings": ELF uses two's complement little-endian for EI_DATA=1):

  0x000  ELF header (e_ehsize=64, e_phoff=64, e_phentsize=56, e_phnum=2,
         e_shnum=0, e_shentsize=64, e_shoff=0, e_type=ET_EXEC, e_machine
         =EM_X86_64=62, e_version=1, e_entry=0x400100, e_flags=0)
  0x040  program header 0: PT_LOAD, flags R+X (p_flags=5), p_offset=0x100,
         p_vaddr=p_paddr=0x400100, p_filesz=0x8, p_memsz=0x8, p_align=0x1000
         (congruent: 0x100 == 0x100 mod 0x1000)
  0x078  program header 1: PT_LOAD, flags R+W (p_flags=6), p_offset=0x108,
         p_vaddr=p_paddr=0x400108, p_filesz=0x10, p_memsz=0x18, p_align=0x1000
         (congruent: 0x108 == 0x108 mod 0x1000)
  0x0B0  zero padding to 0x100
  0x100  seg0 file range: RX code payload, 8 bytes (deterministic pattern)
  0x108  seg1 file range: RW data payload, 16 bytes (deterministic pattern)
  0x118  (unloaded tail: 8 zero bytes; NOT part of any segment -- the BSS
         the loader zero-fills lives at STAGING time: p_memsz-p_filesz)
  0x120  end of file (288 bytes)

Both segments are file-backed by their payloads, so everything the loader
copies is bytes the file actually contains; the intra-page deltas are
nonzero on BOTH segments (0x100 and 0x108), and the entry (0x400100) sits
in the R+X segment, as an executable's should.
"""
import hashlib
import struct
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("kernel/fixtures")

EHDR = struct.Struct("<16sHHIQQQIHHHHHH")  # gABI figure 1-4 / ch4.eheader
PHDR = struct.Struct("<IIQQQQQQ")          # gABI figure 5-1 / ch5.pheader

PT_LOAD, PT_INTERP = 1, 3
EM_X86_64, ET_EXEC = 62, 2

CODE = bytes(range(0x10, 0x18))                    # 8 bytes at 0x100
DATA = bytes((0xA0 + i) & 0xFF for i in range(16))  # 16 bytes at 0x108


def build_base() -> bytearray:
    e = bytearray(0x120)
    ident = bytearray(16)
    ident[0:4] = b"\x7fELF"
    ident[4] = 2   # EI_CLASS = ELFCLASS64
    ident[5] = 1   # EI_DATA  = ELFDATA2LSB
    ident[6] = 1   # EI_VERSION
    ident[7] = 0   # EI_OSABI = SysV
    ident[8] = 0   # EI_ABIVERSION
    ehdr = EHDR.pack(
        bytes(ident), ET_EXEC, EM_X86_64, 1,          # ident, type, machine, version
        0x400100, 64, 0, 0,                           # entry, phoff, shoff, flags
        64, 56, 2, 64, 0, 0,                          # ehsize, phentsize, phnum,
                                                      # shentsize, shnum, shstrndx
    )
    # EHDR is 16s + 13 fields (H H I Q Q Q I H H H H H H) = 64 bytes.
    assert len(ehdr) == 64, len(ehdr)
    e[0:64] = ehdr
    e[0x40:0x40 + PHDR.size] = PHDR.pack(
        PT_LOAD, 5, 0x100, 0x400100, 0x400100, 0x8, 0x8, 0x1000)
    e[0x78:0x78 + PHDR.size] = PHDR.pack(
        PT_LOAD, 6, 0x108, 0x400108, 0x400108, 0x10, 0x18, 0x1000)
    e[0x100:0x108] = CODE
    e[0x108:0x118] = DATA
    # 0x118..0x120 stays zero: it is NOT part of any segment (the BSS the
    # loader zero-fills is the p_memsz-p_filesz tail AT STAGING TIME).
    return e


def patch(b: bytearray, off: int, data: bytes) -> bytearray:
    b2 = bytearray(b)
    b2[off:off + len(data)] = data
    return b2


# One negative per defect class the loader must reject (never load, never
# half-load). Each is the smallest mutation of the valid base image.
def neg_bad_magic(b):
    return patch(b, 0, b"\x7fELG")


def neg_ei_class32(b):
    return patch(b, 4, b"\x01")            # ELFCLASS32 on a 64-bit image


def neg_ei_data_be(b):
    return patch(b, 5, b"\x02")            # ELFDATA2MSB: unsupported encoding


def neg_et_dyn(b):
    return patch(b, 16, struct.pack("<H", 3))   # ET_DYN (PIE): loader is EXEC-only


def neg_phnum_zero(b):
    return patch(b, 0x38, struct.pack("<H", 0)) # claims no program headers


def neg_phentsize(b):
    return patch(b, 0x36, struct.pack("<H", 57))


def neg_memsz_lt_filesz(b):
    return patch(b, 0x78 + 0x28, struct.pack("<Q", 8))  # p_memsz 0x18 -> 0x08


def neg_offset_oob(b):
    return patch(b, 0x78 + 0x08, struct.pack("<Q", 0xFFFFFF))  # p_offset past EOF


def neg_interp(b):
    # The third program header must live IN THE TABLE (e_phoff + 2*56 =
    # 0xB0, inside the zero padding region); appending it at end-of-file
    # would leave the table with a PT_NULL at 0xB0 and no reader would
    # ever see an interp entry.
    b = bytearray(b)
    b[0x38:0x3A] = struct.pack("<H", 3)    # e_phnum 2 -> 3
    b[0xB0:0xB0 + PHDR.size] = PHDR.pack(
        PT_INTERP, 4, 0x108, 0, 0, 0x10, 0x10, 1)
    return b


def neg_entry_oob(b):
    return patch(b, 0x18, struct.pack("<Q", 0x400200))  # outside every segment

NEGATIVES = {
    "bad-magic": neg_bad_magic,
    "ei-class32": neg_ei_class32,
    "ei-data-be": neg_ei_data_be,
    "et-dyn": neg_et_dyn,
    "phnum-zero": neg_phnum_zero,
    "phentsize-wrong": neg_phentsize,
    "memsz-lt-filesz": neg_memsz_lt_filesz,
    "offset-oob": neg_offset_oob,
    "interp-present": neg_interp,
    "entry-oob": neg_entry_oob,
}


def main():
    (OUT / "negative").mkdir(parents=True, exist_ok=True)
    base = build_base()
    (OUT / "elf64-mini").write_bytes(bytes(base))
    print(f"elf64-mini          {len(base):4d} bytes  "
          f"sha256={hashlib.sha256(bytes(base)).hexdigest()}")
    for name, fn in NEGATIVES.items():
        img = fn(base)
        p = OUT / "negative" / f"{name}.elf"
        p.write_bytes(bytes(img))
        print(f"negative/{name + '.elf':22s} {len(img):4d} bytes  "
              f"sha256={hashlib.sha256(bytes(img)).hexdigest()}")


if __name__ == "__main__":
    main()
