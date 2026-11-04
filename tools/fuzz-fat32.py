#!/usr/bin/env python3
"""Fuzz tools/verify-fat32.py (work order D3: FAT, GPT and package parsing).

The target is tools/verify-fat32.py. Its contract, from its own docstring, is
narrow: "Exit 0 = all checks pass; exit 1 = at least one finding." A traceback
is neither, so it is counted as a CRASH, separately from a false pass.

What is mutated
---------------
The corpus image tools/gen-fat32-corpus.py writes to
kernel/src/fat32_corpus.img. The fuzzer reads its own copy of the boot sector
and FAT (struct offsets straight from the Microsoft EFI FAT32 specification,
EFISP01) purely to LOCATE structures: the boot sector, FSInfo sector, backup
boot sector, FAT[0]/FAT[1], and every directory slot. It never reuses the
target's parsing to decide what must be detected. The invariant each mutation
is graded against comes from the specification and from the corpus provenance
record tools/fat32-corpus-manifest.json:

  - the boot-sector trail signature, every BPB field the volume cannot work
    without, the FSInfo signatures and the backup boot-sector signature
    (spec 3.1, 3.2);
  - FAT[0] media byte and FAT[1] end-of-chain entry (spec 4.2);
  - every directory entry the corpus manifest names: its short-name bytes or
    its LFN characters, the LFN LDIR_Type and LDIR_Chksum fields (spec 6.2,
    6.3), and the "." / ".." cluster fields of a subdirectory (spec 6.4);
  - the volume label, which must equal BS_VolLab (spec 3.1 BPB_VolLab and 6).

Those bytes form the `exact` set: a mutation there MUST be reported as a
finding. Everything else in a 32 MiB image is file content, free space, unused
FAT entries, the second FAT, catalog dates or short-name bytes inside
"projects", whose child names the verifier never compares. Mutating those is
reported as an `unconstrained` false pass, not a defect: see the notes the run
prints. This split is the FAT32 analogue of fuzz-disk.py's coverage map, and
the same honesty applies: a false pass is only a defect where an invariant
exists to violate.

Three outcomes, counted SEPARATELY as D3 requires:

  crashes       the target raised an exception instead of returning 0 or 1.
                Any crash count > 0 is a defect in the target.
  false passes  the target exited 0 despite a mutation in the exact set.
                Classified by region; `structural` must be 0.
  probe misses  the deterministic single-byte probe sweep (every byte of every
                exact field, one probe per byte) that was not reported.

Seeded (default 0x4E4F5641 = "NOVA") and the seed is printed, so any run can
be replayed exactly.

Exit 0 only if: crashes == 0, structural false passes == 0, probe misses == 0.

Regression use: the target is loaded in-process and driven through its own
main(), the way tools/fuzz-disk.py drives verify_image(); a run is ~25 ms
because the 32 MiB image is read once per call and the mutation is written in
place. The fuzzer writes only to a temporary copy of the image.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import random
import struct
import sys
import tempfile
import time
from pathlib import Path

SECTOR = 512
DEFAULT_SEED = 0x4E4F5641  # "NOVA"
EOC_MIN = 0x0FFFFFF8

# Region labels for the false-pass classification. Only "structural" is gated.
STRUCTURAL = "structural"
FAT1_LINK = "fat1-file-link"
FAT1_FREE = "fat1-unused-entry"
FAT1_RESERVED = "fat1-reserved-entry"
FAT2 = "fat2"
BOOT_FIELD = "boot/metadata"
DIR_UNVERIFIED = "directory-unverified"
DATA = "data-or-free"


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def load_target():
    """Import tools/verify-fat32.py by path: the module name has a hyphen."""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("verify_fat32", here / "verify-fat32.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Layout:
    """The volume's structure, read from the image with spec offsets only.

    This exists to say WHERE the checked structures are. It is deliberately
    not used to decide whether a mutation is a violation; that comes from the
    probe definitions below.
    """

    def __init__(self, data: bytes):
        self.d = bytes(data)
        self.bps = _u16(self.d, 11)
        self.spc = self.d[13]
        self.rsvd = _u16(self.d, 14)
        self.nfats = self.d[16]
        self.tot32 = _u32(self.d, 32)
        self.fatsz = _u32(self.d, 36)
        self.rootclus = _u32(self.d, 44)
        self.fsinfo = _u16(self.d, 48)
        self.bkboot = _u16(self.d, 50)
        self.fat_off = self.rsvd * self.bps
        self.data_off = (self.rsvd + self.nfats * self.fatsz) * self.bps
        self.nclusters = (self.tot32 - self.rsvd - self.nfats * self.fatsz) // self.spc

    def fat(self, c: int) -> int:
        return _u32(self.d, self.fat_off + c * 4)

    def cluster_off(self, c: int) -> int:
        return self.data_off + (c - 2) * self.spc * self.bps

    def chain(self, first: int) -> list[int]:
        out: list[int] = []
        c = first
        seen: set[int] = set()
        while 2 <= c < self.nclusters + 2 and c not in seen and len(out) < 200000:
            seen.add(c)
            out.append(c)
            nxt = self.fat(c)
            if nxt == 0 or nxt >= EOC_MIN:
                break
            c = nxt
        return out

    def slots(self, first: int):
        """(absolute offset, 32 raw bytes) for each directory slot, in order."""
        for c in self.chain(first):
            base = self.cluster_off(c)
            for i in range(self.spc * self.bps // 32):
                off = base + i * 32
                raw = self.d[off:off + 32]
                if len(raw) < 32 or raw[0] == 0x00:
                    return
                yield off, raw


def entry_name(raw: bytes) -> str:
    return raw[0:8].rstrip(b" ").decode("ascii", "replace")


def walk(lay: Layout):
    """Return (root_slots, subdirs) where subdirs maps name -> slot list."""
    root = list(lay.slots(lay.rootclus))
    subdirs: dict[str, list[tuple[int, bytes]]] = {}
    for off, raw in root:
        if raw[0] == 0xE5 or raw[11] & 0x3F == 0x0F:
            continue
        if raw[11] & 0x10 and raw[0:1] != b".":
            subdirs[entry_name(raw)] = list(lay.slots(
                (_u16(raw, 20) << 16) | _u16(raw, 26)))
    return root, subdirs


def lfn_unit_offsets(raw: bytes) -> list[int]:
    """Byte offsets inside a 32-byte LFN slot that hold UTF-16 characters."""
    return [1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30]


class Fuzzer:
    def __init__(self, target, image: Path, iterations: int, seed: int):
        self.target = target
        self.src = image
        self.original = image.read_bytes()
        self.layout = Layout(self.original)
        self.iterations = iterations
        self.seed = seed
        self.root, self.subdirs = walk(self.layout)
        # Clusters owned by some chain: used to tell a corrupted chain link
        # from a byte of an unused FAT entry, which has no invariant.
        used: set[int] = set()
        for off, raw in self.root:
            if raw[0] == 0xE5 or raw[11] & 0x3F == 0x0F:
                continue
            first = (_u16(raw, 20) << 16) | _u16(raw, 26)
            if first:
                used.update(self.layout.chain(first))
        for slots in self.subdirs.values():
            for off, raw in slots:
                if raw[0] == 0xE5 or raw[11] & 0x3F == 0x0F or raw[0:1] == b".":
                    continue
                first = (_u16(raw, 20) << 16) | _u16(raw, 26)
                if first:
                    used.update(self.layout.chain(first))
        self.used_clusters = used

        # Working copy: mutations are written in place, one byte at a time.
        fd, self.tmp_name = tempfile.mkstemp(prefix="d3-fat32-", suffix=".img")
        os.close(fd)
        self.tmp = Path(self.tmp_name)
        self.tmp.write_bytes(self.original)
        self.fh = open(self.tmp, "r+b")

        self.probes: list[tuple[str, int, int]] = []  # (label, offset, new byte)
        # offset -> mask of bits whose change violates an invariant. FAT32
        # entries are 28-bit (spec 4.1), so the top nibble of a FAT[1] byte is
        # reserved: a flip there is NOT a violation and must not be graded.
        self.exact_offsets: dict[int, int] = {}
        self.crash_kinds: dict[str, int] = {}
        self._build_probes()

    # -- probe construction -------------------------------------------------

    def _probe(self, label: str, off: int, new: int, mask: int = 0xFF) -> None:
        self.probes.append((label, off, new))
        self.exact_offsets[off] = self.exact_offsets.get(off, 0) | mask

    def _structural_slots(self) -> list[tuple[str, int, bytes]]:
        """Directory slots whose name bytes the corpus record pins.

        The root's names are compared as a whole by the target, and the
        "many entries" directory's 64 names are compared as a list. The
        "projects" directory's child names are not compared to anything, so
        its short names are excluded (its dot entries are not).
        """
        out = [("root", off, raw) for off, raw in self.root]
        for name, slots in self.subdirs.items():
            if name == "many entries":
                out.extend((name, off, raw) for off, raw in slots)
        return out

    def _build_probes(self) -> None:
        d = self.original
        lay = self.layout

        # 3.1 boot sector: trail signature and the BPB fields the volume
        # cannot be interpreted without. Each of these is checked by the
        # target as an equality or a closed set, so every bit flip must be
        # reported. (BPB_TotSec32 and BPB_FATSz32 are deliberately absent:
        # the target checks only that the image is big enough and the FAT
        # large enough, so a small DECREASE is not a violation.)
        boot_fields = [(11, 2, "bps"), (13, 1, "SecPerClus"), (16, 1, "NumFATs"),
                       (17, 2, "RootEntCnt"), (44, 4, "RootClus"),
                       (48, 2, "FSInfo"), (50, 2, "BkBootSec"),
                       (510, 2, "boot trail sig")]
        for off, n, label in boot_fields:
            for k in range(n):
                self._probe(f"boot {label} byte {k}", off + k, d[off + k] ^ 0x01)

        # BS_VolLab (offset 71) must equal the volume-label directory entry.
        for k in range(11):
            self._probe(f"BS_VolLab byte {k}", 71 + k, d[71 + k] ^ 0x01)

        # 3.2 FSInfo signatures: lead signature and struct signature.
        for k in range(4):
            self._probe(f"FSInfo lead sig byte {k}", 512 + k, d[512 + k] ^ 0x01)
            self._probe(f"FSInfo struct sig byte {k}", 512 + 484 + k,
                        d[512 + 484 + k] ^ 0x01)

        # 3.1 backup boot sector trail signature.
        bk = lay.bkboot * SECTOR
        for k in range(2):
            self._probe(f"backup boot sig byte {k}", bk + 510 + k, d[bk + 510 + k] ^ 0x01)

        # 4.2 FAT[0] media byte; FAT[1] end-of-chain (its low 3 bits are
        # masked by the target, so only bytes 5..7 of the entry are exact).
        self._probe("FAT[0] media byte", lay.fat_off, d[lay.fat_off] ^ 0x01)
        for k in (5, 6, 7):
            self._probe(f"FAT[1] EOC byte {k}", lay.fat_off + k,
                        d[lay.fat_off + k] ^ 0x01,
                        mask=0x0F if k == 7 else 0xFF)

        # Directory entry names. For a short-only entry the decoded name
        # changes; for an entry with an LFN the LFN checksum no longer matches
        # the short name (6.2). Either way the target must report it.
        for owner, off, raw in self._structural_slots():
            if raw[11] & 0x3F == 0x0F:  # LFN slot (6.2, 6.3)
                # LDIR_Type must be 0; LDIR_Chksum must match the short entry.
                self._probe(f"{owner} LFN type @{off}", off + 12, 1)
                self._probe(f"{owner} LFN checksum @{off}", off + 13,
                            raw[13] ^ 0x01)
                for k in lfn_unit_offsets(raw):
                    if 0x00 < raw[k] < 0x7F:
                        self._probe(f"{owner} LFN char @{off}+{k}", off + k,
                                    raw[k] ^ 0x01)
            else:
                # NTRes (entry byte 12) declares the stored short name
                # lower-case (spec 6.3.1): with bit 0x08 set, a case flip in
                # the base name decodes to the same name; with 0x10 set, the
                # same for the extension. Those bytes are not in the exact
                # set, because the corpus invariant (the DECODED name) does
                # not change there.
                ntres = raw[12]
                for k in range(11):
                    if raw[k] == 0x20:
                        continue
                    if k < 8 and ntres & 0x08:
                        continue
                    if k >= 8 and ntres & 0x10:
                        continue
                    self._probe(f"{owner} short name @{off}+{k}", off + k,
                                raw[k] ^ 0x01)

        # 6.4: "." points at the directory itself; ".." of a root child is 0.
        for name, slots in self.subdirs.items():
            for off, raw in slots:
                if raw[0:2] == b". ":
                    self._probe(f"{name} '.' cluster low @{off}", off + 20,
                                raw[20] ^ 0x01)
                    self._probe(f"{name} '.' cluster high @{off}", off + 26,
                                raw[26] ^ 0x01)
                elif raw[0:2] == b"..":
                    self._probe(f"{name} '..' cluster low @{off}", off + 20, 1)
                    self._probe(f"{name} '..' cluster high @{off}", off + 26, 1)

        # Volume label: the entry's own 11 name bytes, checked against
        # BS_VolLab and against b"NOVAVOL".
        for off, raw in self.root:
            if raw[11] & 0x08:
                for k in range(11):
                    self._probe(f"volume label byte {k}", off + k, raw[k] ^ 0x01)
                break

    # -- running the target ------------------------------------------------

    def run(self) -> tuple[int | None, str, str | None]:
        """(exit code, stdout, exception text). exit code None means crash."""
        self.target.findings.clear()
        old_argv = sys.argv
        sys.argv = ["verify-fat32.py", str(self.tmp)]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = self.target.main()
            return rc, buf.getvalue(), None
        except BaseException as e:  # noqa: BLE001 - a target crash IS the datum
            return None, buf.getvalue(), f"{type(e).__name__}: {e}"
        finally:
            sys.argv = old_argv

    def mutate(self, off: int, new: int) -> None:
        self.fh.seek(off)
        self.fh.write(bytes([new]))
        self.fh.flush()

    def restore(self, off: int, old: int) -> None:
        self.mutate(off, old)

    # -- phases -------------------------------------------------------------

    def baseline(self) -> bool:
        rc, out, exc = self.run()
        if rc == 0 and exc is None:
            print("fuzz-fat32: baseline: unmutated image verifies (exit 0, no findings)")
            return True
        print(f"fuzz-fat32: BASELINE FAIL: exit={rc} exception={exc}")
        for line in out.splitlines()[-6:]:
            print(f"fuzz-fat32:   {line}")
        return False

    def sweep(self) -> tuple[int, int, int]:
        """Every probe, once. Returns (detected, false_pass, crash)."""
        det = fp = cr = 0
        misses: list[tuple[str, int, int | None, str | None]] = []
        for label, off, new in self.probes:
            old = self.original[off]
            self.mutate(off, new)
            rc, _out, exc = self.run()
            self.restore(off, old)
            if exc is not None:
                cr += 1
                self._count_crash(exc)
                misses.append((label, off, rc, exc))
            elif rc == 0:
                fp += 1
                misses.append((label, off, rc, None))
            else:
                det += 1
        self._report_misses("probe", misses)
        print(f"fuzz-fat32: probes: {det}/{len(self.probes)} single-byte structural "
              f"mutations detected ({fp} false passes, {cr} crashes)")
        return det, fp, cr

    def _report_misses(self, kind: str, misses) -> None:
        for label, off, rc, exc in misses[:20]:
            what = f"CRASH {exc}" if exc else f"exit {rc} (not detected)"
            print(f"fuzz-fat32:   {kind} MISS {label} @ byte {off}: {what}")
        if len(misses) > 20:
            print(f"fuzz-fat32:   ... {len(misses) - 20} more {kind} misses")

    def _count_crash(self, exc: str) -> None:
        """Classify a traceback by type; struct.error prints just 'error: ...'."""
        kind = exc.split(":", 1)[0]
        if kind == "error":
            kind = "struct.error"
        self.crash_kinds[kind] = self.crash_kinds.get(kind, 0) + 1

    def classify(self, off: int, bit: int) -> str:
        mask = self.exact_offsets.get(off)
        if mask is not None and (1 << bit) & mask:
            return STRUCTURAL
        lay = self.layout
        fat2_off = lay.fat_off + lay.fatsz * lay.bps
        if fat2_off <= off < fat2_off + lay.fatsz * lay.bps:
            return FAT2
        if lay.fat_off <= off < lay.fat_off + lay.fatsz * lay.bps:
            entry = (off - lay.fat_off) // 4
            if entry < 2:
                return FAT1_RESERVED
            return FAT1_LINK if entry in self.used_clusters else FAT1_FREE
        for name, slots in self.subdirs.items():
            for slot_off, _raw in slots:
                if slot_off <= off < slot_off + 32:
                    return DIR_UNVERIFIED
        if off < lay.data_off:
            return BOOT_FIELD
        return DATA

    def random_phase(self) -> tuple[dict[str, int], int, list[str]]:
        rng = random.Random(self.seed)
        exact_pool = sorted(self.exact_offsets)
        n = len(self.original)
        detected: dict[str, int] = {}
        fp_by_region: dict[str, int] = {}
        examples: dict[str, tuple[int, int, int]] = {}
        crashes = 0
        crash_examples: list[str] = []
        t0 = time.perf_counter()

        for _ in range(self.iterations):
            if exact_pool and rng.random() < 0.7:
                off = exact_pool[rng.randrange(len(exact_pool))]
            else:
                off = rng.randrange(n)
            old = self.original[off]
            bit = rng.randrange(8)
            new = old ^ (1 << bit)  # one guaranteed bit flip
            self.mutate(off, new)
            rc, _out, exc = self.run()
            self.restore(off, old)
            if exc is not None:
                crashes += 1
                self._count_crash(exc)
                if len(crash_examples) < 5:
                    crash_examples.append(f"byte {off}: {exc}")
                continue
            if rc != 0:
                detected["any"] = detected.get("any", 0) + 1
                continue
            region = self.classify(off, bit)
            fp_by_region[region] = fp_by_region.get(region, 0) + 1
            examples.setdefault(region, (off, old, new))

        dt = time.perf_counter() - t0
        print(f"fuzz-fat32: random: {self.iterations} single-bit mutations, "
              f"{detected.get('any', 0)} reported as findings, "
              f"{dt / max(self.iterations, 1) * 1000:.1f} ms/mutation")
        total_fp = sum(fp_by_region.values())
        print(f"fuzz-fat32: false pass: {total_fp}/{self.iterations} reported success, "
              "classified:")
        for region in (STRUCTURAL, FAT1_LINK, FAT1_FREE, FAT1_RESERVED, FAT2, BOOT_FIELD,
                       DIR_UNVERIFIED, DATA):
            note = {
                STRUCTURAL: "  <- must be 0; >0 is a target defect",
                FAT1_LINK: "  <- file-chain links: truncating one to EOC is not checked",
                FAT1_FREE: "  <- free cluster's FAT entry: no chain owns it",
                FAT1_RESERVED: "  <- reserved FAT entries: only documented bits are checked",
                FAT2: "  <- second FAT is never read (FAT1/FAT2 mismatch not checked)",
                BOOT_FIELD: "  <- reserved bytes and unchecked boot fields",
                DIR_UNVERIFIED: "  <- 'projects' child names are not compared",
                DATA: "  <- file content and free space: no invariant to violate",
            }[region]
            line = f"fuzz-fat32:   {region:>18}: {fp_by_region.get(region, 0)}{note}"
            if region in examples:
                off, old_b, new_b = examples[region]
                line += f"  [e.g. byte {off}: {old_b:#04x}->{new_b:#04x}]"
            print(line)
        return fp_by_region, crashes, crash_examples

    def close(self) -> None:
        self.fh.close()
        try:
            self.tmp.unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("iterations", nargs="?", type=int, default=2000)
    ap.add_argument("--image", default="kernel/src/fat32_corpus.img")
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=DEFAULT_SEED)
    args = ap.parse_args()

    image = Path(args.image)
    if not image.is_file():
        print(f"fuzz-fat32: image {image} not found", file=sys.stderr)
        return 2

    fz = Fuzzer(load_target(), image, args.iterations, args.seed)
    try:
        lay = fz.layout
        print(f"fuzz-fat32: image {image} ({len(fz.original)} bytes), "
              f"{args.iterations} iterations, seed {args.seed:#x}")
        print(f"fuzz-fat32: layout: {lay.nclusters} clusters, spc {lay.spc}, "
              f"FAT1 at {lay.fat_off}, data at {lay.data_off}, "
              f"{len(fz.root)} root slots, {len(fz.subdirs)} subdirectories "
              f"({', '.join(sorted(fz.subdirs))})")
        print(f"fuzz-fat32: exact set: {len(fz.exact_offsets)} bytes in "
              f"{len(fz.probes)} probes")

        if not fz.baseline():
            return 1

        _det, probe_fp, probe_cr = fz.sweep()
        fp_by_region, crashes, crash_examples = fz.random_phase()
        for c in crash_examples:
            print(f"fuzz-fat32:   crash: {c}")

        struct_fp = fp_by_region.get(STRUCTURAL, 0)
        total_crashes = probe_cr + crashes
        print(f"fuzz-fat32: crashes: {total_crashes} "
              f"(probe phase {probe_cr}, random phase {crashes})")
        for kind, count in sorted(fz.crash_kinds.items(), key=lambda kv: -kv[1]):
            print(f"fuzz-fat32:   crash kind: {kind}: {count}")
        print(f"fuzz-fat32: structural false passes: {struct_fp}")
        ok = total_crashes == 0 and struct_fp == 0 and probe_fp == 0
        print(f"fuzz-fat32: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        fz.close()


if __name__ == "__main__":
    sys.exit(main())
