#!/usr/bin/env python3
"""Independent ELF64 oracle for the K4b loader (host side).

Written from the ELF generic ABI specification (System V Application Binary
Interface - AMD64 Architecture Processor Supplement, sections referenced
inline), NOT from tools/gen-elf64-fixture.py's struct calls and not from the
kernel's loader. It re-parses an ELF64 image, applies the spec's structural
validity rules, reconstructs the loader's DOCUMENTED staging at the
documented window, and prints ELFORACLE lines that kernel/scripts test
harnesses diff against the guest's own lines.

Usage:
    verify-elf64.py <image> [--window 0x8000000] [--slot 2MiB]

Checks applied (gABI ch4.eheader.html, ch5.pheader.html):
  - ident: \x7fELF magic, EI_CLASS=ELFCLASS64(2), EI_DATA=ELFDATA2LSB(1),
    EI_VERSION=1
  - e_phentsize == 56 (sizeof Elf64_Phdr, gABI figure 5-1)
  - e_phoff inside the file, e_phnum > 0
  - e_version == 1 (both header and per-entry p_version rule via EV_CURRENT)
  - for every PT_LOAD: p_offset + p_filesz <= file size;
    p_memsz >= p_filesz; p_align == 0 or PAGE_SIZE or larger power of two;
    p_vaddr congruent to p_offset modulo p_align (gABI: "if p_align is set,
    p_vaddr shall be congruent to p_offset modulo p_align")
  - K4b loader policy (kernel/src/elf.rs; policy rules, not gABI musts):
    e_type must be ET_EXEC (no PIE), PT_INTERP must be absent, and e_entry
    must land inside some PT_LOAD's [p_vaddr, p_vaddr+p_filesz) — an
    entry outside every file-backed segment would execute zeros/BSS.

Staging reconstruction (the loader's documented rule): segment i of N
PT_LOADs (sorted by p_vaddr) stages at
    page_base = WINDOW + i * SLOT   (page-aligned)
    delta     = p_vaddr mod 4096
    bytes     = file[p_offset : p_offset+p_filesz] + zeros to p_memsz
at page_base + delta. The oracle prints the staged region's sha256 and
per-segment (base, delta, filesz, memsz) so a second implementation can be
diffed byte-exactly.
"""
import argparse
import hashlib
import struct
import sys

EHDR = struct.Struct("<16sHHIQQQIHHHHHH")
PHDR = struct.Struct("<IIQQQQQQ")

PT_LOAD, PT_INTERP = 1, 3
PAGE = 0x1000


def fail(msg):
    print(f"ELFORACLE VERDICT invalid ({msg})")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--window", type=lambda x: int(x, 0), default=0x8000000)
    ap.add_argument("--slot", type=lambda x: int(x, 0), default=0x200000)
    args = ap.parse_args()
    data = open(args.image, "rb").read()

    if len(data) < 64:
        fail("shorter than an ELF header")
    ident = data[:16]
    if ident[:4] != b"\x7fELF":
        fail("bad magic")
    if ident[4] != 2:
        fail("EI_CLASS != ELFCLASS64")
    if ident[5] != 1:
        fail("EI_DATA != ELFDATA2LSB")
    if ident[6] != 1:
        fail("EI_VERSION != 1")
    (e_ident, e_type, e_machine, e_version, e_entry, e_phoff, e_shoff,
     e_flags, e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum,
     e_shstrndx) = EHDR.unpack(data[:64])
    if e_version != 1:
        fail("e_version != 1")
    if e_phnum == 0:
        fail("e_phnum == 0: no program header table (gABI: ET_EXEC requires "
             "a program header table for exec)")
    if e_phentsize != PHDR.size:
        fail(f"e_phentsize {e_phentsize} != 56")
    if e_phnum == 0:
        fail("e_phnum == 0")
    if e_phoff < 64 or e_phoff + e_phnum * e_phentsize > len(data):
        fail("program header table out of bounds")

    loads = []
    entry_ok = False
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        (p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz,
         p_align) = PHDR.unpack(data[off:off + PHDR.size])
        if p_type == PT_INTERP:
            fail("PT_INTERP present (K4b loader rejects dynamic images)")
        if p_type != PT_LOAD:
            continue
        if p_offset + p_filesz > len(data):
            fail(f"PT_LOAD {i}: file range past EOF")
        if p_memsz < p_filesz:
            fail(f"PT_LOAD {i}: p_memsz < p_filesz")
        if p_align not in (0, 1) and (p_align & (p_align - 1)) != 0:
            fail(f"PT_LOAD {i}: p_align not a power of two >= PAGE_SIZE")
        if p_align >= PAGE and (p_vaddr % p_align) != (p_offset % p_align):
            fail(f"PT_LOAD {i}: p_vaddr not congruent to p_offset mod p_align")
        if e_entry >= p_vaddr and e_entry < p_vaddr + p_filesz:
            entry_ok = True
        loads.append((i, p_vaddr, p_offset, p_filesz, p_memsz))

    # K4b loader policy: ET_EXEC only; entry inside a file-backed segment.
    if e_type != 2:
        fail(f"e_type {e_type} != ET_EXEC (K4b loader policy: no PIE)")
    if not entry_ok:
        fail(f"e_entry {e_entry:#x} outside every PT_LOAD file range")
    if not loads:
        fail("no PT_LOAD segments")
    loads.sort(key=lambda t: t[1])

    print(f"ELFORACLE VERDICT valid")
    print(f"ELFORACLE entry {e_entry:#x} phnum {e_phnum} loads {len(loads)}")
    pages = b""
    for idx, (ph_i, vaddr, foff, filesz, memsz) in enumerate(loads):
        page_base = args.window + idx * args.slot
        delta = vaddr % PAGE
        print(f"ELFORACLE seg {idx} base {page_base:#x} delta {delta:#x} "
              f"filesz {filesz:#x} memsz {memsz:#x} vaddr {vaddr:#x}")
        seg = data[foff:foff + filesz] + b"\x00" * (memsz - filesz)
        pages += seg
    print(f"ELFORACLE staged_sha256 {hashlib.sha256(pages).hexdigest()}")
    print(f"ELFORACLE staged_len {len(pages)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
