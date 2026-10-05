#!/usr/bin/env python3
"""C3 atomic install: A/B kernel slots over an unmodified Limine boot chain.

Redesign of docs/atomic-install-design.md (the 2026-09-22 pointer-block
design is void per docs/audit-2026-09-24.md: it updated only a CRC and never
the slot field, and Limine cannot read a custom pointer block). This
implementation uses only what the pinned, unmodified Limine 12.9.0 actually
reads at boot:

  * The boot entry point is the /limine.conf file on the ESP (plus its
    /EFI/BOOT/LIMINE.CONF twin, which BIOS builds keep byte-identical).
  * The ATOMIC POINTER is that file's FAT directory entry: the swap writes
    one 512-byte directory sector whose only change is the entry's first
    cluster number and size. Before the swap, the new release is fully
    staged into fresh, previously-never-used clusters; a process killed at
    any point leaves the pointer at the old or the new chain - both boot.
  * Slots are /NUCLEUS.A and /NUCLEUS.B (8.3 names, no LFN). Each release's
    config is an anonymous cluster chain pinning its slot kernel with the
    C4 mechanism: path: boot(1):/<file>#<128-hex blake2b-512>. Limine itself
    verifies that hash at every boot, so a booted kernel always matches the
    active config's pin regardless of what wrote the pointer.
  * The factory /NUCLEUS kernel and the factory config chain are never
    modified or deallocated; they are the deep rollback target recorded in
    /SLOTMAP.TXT at first install.
  * Allocation is append-only: clusters at or below the highest cluster ever
    used (the watermark persisted in /SLOTMAP.TXT) are never reused, so a
    rollback target chain can never be overwritten by a later install.
    A compaction step is future work; the ESP has room for ~200 releases.
  * The trust input is a C2 signed manifest (tools/nova_trust.py): the
    manifest's signature, structure and payload hashes are verified before
    anything is written, and the host replay state advances only after the
    commit is on disk and read back correct.

Interruption model, exactly: the installer performs a fixed sequence of
logical operations (printed as C3-OP lines). --fault-point kills the process
(os._exit(137), no cleanup, by design - a crashed installer must not roll
back) before a named operation, or mid-way through a content write. scripts/
test-atomic-install.sh boots every resulting image state under QEMU and
requires NOVA_BOOT_OK from each: that is the "failed or interrupted install
always leaves a bootable machine" gate.

Honest limitations (also in docs/atomic-install-design.md):
  * The commit is one 512-byte sector write per config entry, root first and
    the /EFI/BOOT twin second. A process kill between the two leaves BIOS
    booting the new release and UEFI the old one; both boot, and the next
    successful install re-points both. Power-loss TORN SECTOR writes are not
    survivable on a raw FAT volume (no journal); the root directory sector
    holding /limine.conf also holds other root entries. Mitigations (C6
    read-only store, or the K4 in-kernel package verifier owning this
    decision) are future work and are not claimed here.
  * /SLOTMAP.TXT is an unsigned hint file. Every row is re-verified against
    image content (chain sha256 + the pinned kernel's blake2b) before use;
    a stale or corrupted slotmap makes rollback refuse loudly rather than
    guess. It does not protect against an attacker who can rewrite the
    disk; it protects against interruption and accident.

NOVA_C3_MUTANT=skip-config-stage is a deliberate bug used only by the
sabotage gate in scripts/test-atomic-install.sh: it skips staging the new
config content and commits the pointer at an unwritten chain. The suite
requires the resulting image to FAIL to boot.

Usage:
  py -3 tools/atomic_install.py install --image IMG --manifest M.json
        --payload-dir DIR --trust-key PUB.pem --state-file STATE.json
        [--fault-point SPEC]
  py -3 tools/atomic_install.py status  --image IMG
  py -3 tools/atomic_install.py rollback --image IMG
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_trust as nt  # noqa: E402  (C2 trust: signing/verification/state)

SECTOR = 512
ESP_TYPE_GUID = bytes.fromhex("2832ac12f81fd211ba4b00a0c93ec93b")
FACTORY_KERNEL = "/NUCLEUS"
SLOT_FILES = {"A": "/NUCLEUS.A", "B": "/NUCLEUS.B"}
SLOTMAP_NAME = "slotmap.txt"  # stored as 8.3 SLOTMAP.TXT
KERNEL_PATH_RE = re.compile(
    rb"(?m)^(\s*path:\s*)boot\(1\):/NUCLEUS(#[0-9a-f]{128})?\s*$")
SEQ_COMMENT_RE = re.compile(rb"(?m)^# nova-release-sequence: ([0-9]+)\s*$")
VER_COMMENT_RE = re.compile(rb"(?m)^# nova-release-version: (.*)\s*$")

OPS = [
    "stage_kernel_content", "stage_kernel_fat", "stage_kernel_dirent",
    "stage_config_content", "stage_config_fat",
    "stage_slotmap_content", "stage_slotmap_fat", "stage_slotmap_dirent",
    "sync_stage", "commit_root_ptr", "commit_efi_ptr", "sync_commit",
    "write_state",
]


class InstallError(Exception):
    """Fail-closed refusal. Nothing has been committed when this raises."""


def blake2b_512(data: bytes) -> str:
    """The digest form Limine's URI parser expects: 128 lowercase hex chars,
    no algorithm prefix (vendored limine-12.9.0 common/lib/uri.c:65-90)."""
    return hashlib.blake2b(data, digest_size=64).hexdigest()


# ------------------------------------------------------------------- image --

class Fat16Image:
    """Read/write access to the FAT16 ESP inside a GPT image file.

    Parses the GPT (UEFI 2.10 §5.3.1: entries at LBA 2, 128 x 128 B) to find
    the EFI System Partition, then the FAT16 BPB. All mutation goes through
    small explicit primitives so the fault-point harness can interleave.
    """

    def __init__(self, path: Path):
        self.path = path
        self.f = open(path, "r+b")
        self._locate_esp()
        self._parse_bpb()

    # -- low level -----------------------------------------------------
    def pread(self, off: int, n: int) -> bytes:
        self.f.seek(off)
        data = self.f.read(n)
        if len(data) != n:
            raise InstallError(f"{self.path}: short read at {off}")
        return data

    def pwrite(self, off: int, data: bytes) -> None:
        self.f.seek(off)
        self.f.write(data)

    def flush(self) -> None:
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.close()

    # -- layout --------------------------------------------------------
    def _locate_esp(self) -> None:
        hdr = self.pread(SECTOR, 92)
        if hdr[0:8] != b"EFI PART":
            raise InstallError("not a GPT image (no EFI PART signature at LBA 1)")
        entry_lba, entry_count, entry_size = struct.unpack_from("<QII", hdr, 72)
        if entry_count < 1 or entry_size != 128:
            raise InstallError(f"unexpected GPT entry geometry: {entry_count} x {entry_size}")
        for i in range(entry_count):
            e = self.pread(entry_lba * SECTOR + i * 128, 128)
            if e[0:16] == ESP_TYPE_GUID:
                first, last = struct.unpack_from("<QQ", e, 32)
                self.esp_off = first * SECTOR
                self.esp_lbas = last - first + 1
                return
        raise InstallError("no EFI System Partition in the GPT")

    def _parse_bpb(self) -> None:
        bpb = self.pread(self.esp_off, SECTOR)
        if bpb[510:512] != b"\x55\xAA":
            raise InstallError("ESP boot sector has no 55AA signature")
        self.bps = struct.unpack_from("<H", bpb, 11)[0]
        self.spc = bpb[13]
        self.reserved = struct.unpack_from("<H", bpb, 14)[0]
        self.nfats = bpb[16]
        self.root_entries = struct.unpack_from("<H", bpb, 17)[0]
        self.fat_sectors = struct.unpack_from("<H", bpb, 22)[0]
        if self.bps != SECTOR or self.spc < 1 or self.nfats != 2:
            raise InstallError(
                f"unsupported FAT geometry: bps={self.bps} spc={self.spc} nfats={self.nfats}")
        self.fat_off = self.esp_off + self.reserved * SECTOR
        self.root_off = self.fat_off + self.nfats * self.fat_sectors * SECTOR
        self.root_bytes = self.root_entries * 32
        self.data_off = self.root_off + self.root_bytes
        self.total_clusters = ((self.esp_lbas * SECTOR - (self.data_off - self.esp_off))
                               // (self.spc * SECTOR))
        self.max_cluster = 1 + self.total_clusters  # clusters are 2..max_cluster

    def cluster_off(self, c: int) -> int:
        if not (2 <= c <= self.max_cluster):
            raise InstallError(f"cluster {c} out of range 2..{self.max_cluster}")
        return self.data_off + (c - 2) * self.spc * SECTOR

    def cluster_bytes(self) -> int:
        return self.spc * SECTOR

    # -- FAT -----------------------------------------------------------
    def fat_entry(self, c: int, copy: int = 0) -> int:
        off = self.fat_off + copy * self.fat_sectors * SECTOR + c * 2
        return struct.unpack("<H", self.pread(off, 2))[0]

    def set_fat_entry(self, c: int, value: int) -> None:
        """Update one FAT entry in BOTH copies (ECMA-107 §2.4 / MS-FAT §3.2:
        the copies must be kept identical)."""
        raw = struct.pack("<H", value)
        for copy in range(self.nfats):
            off = self.fat_off + copy * self.fat_sectors * SECTOR + c * 2
            self.pwrite(off, raw)

    def check_fat_copies(self) -> None:
        fat0 = self.pread(self.fat_off, self.fat_sectors * SECTOR)
        for copy in range(1, self.nfats):
            fatn = self.pread(self.fat_off + copy * self.fat_sectors * SECTOR,
                              self.fat_sectors * SECTOR)
            if fat0 != fatn:
                raise InstallError(f"FAT copies 0 and {copy} differ; refusing to install")

    def follow_chain(self, first: int, size: int) -> bytes:
        out = bytearray()
        c = first
        seen = set()
        while True:
            if c in seen:
                raise InstallError("cluster chain loop")
            seen.add(c)
            out += self.pread(self.cluster_off(c), self.cluster_bytes())
            nxt = self.fat_entry(c)
            if nxt >= 0xFFF8:
                break
            if nxt < 2:
                raise InstallError(f"chain broke at cluster {c} (entry {nxt:#06x})")
            c = nxt
        return bytes(out[:size])

    def chain_clusters(self, first: int) -> list[int]:
        out, c, seen = [], first, set()
        while True:
            if c in seen:
                raise InstallError("cluster chain loop")
            seen.add(c)
            out.append(c)
            nxt = self.fat_entry(c)
            if nxt >= 0xFFF8:
                return out
            if nxt < 2:
                raise InstallError(f"chain broke at cluster {c}")
            c = nxt

    # -- allocation (append-only watermark policy) ----------------------
    def high_water(self) -> int:
        """Highest cluster referenced by any directory entry chain."""
        hi = 1
        for slot in range(self.root_entries):
            e = self.pread(self.root_off + slot * 32, 32)
            if e[0] == 0x00:
                continue  # end-of-directory; entries are packed from slot 0
            if e[11] & 0x3F and not (e[11] & 0x08):
                pass  # LFN or normal; both carry a cluster number (0 for LFN)
            first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
            size = struct.unpack_from("<I", e, 28)[0]
            if first >= 2 and size > 0:
                try:
                    hi = max(hi, max(self.chain_clusters(first)))
                except InstallError:
                    pass  # a damaged non-critical entry must not block the scan
        return hi

    def alloc_after(self, watermark: int, nclusters: int) -> list[int]:
        """Allocate nclusters, ALL strictly above `watermark`. Chains below
        the watermark belong to past generations or the rollback target and
        are never touched, even if their FAT entries read as free."""
        got = []
        c = watermark + 1
        while len(got) < nclusters:
            if c > self.max_cluster:
                raise InstallError("ESP out of clusters above the watermark")
            if self.fat_entry(c) == 0x0000:
                got.append(c)
            c += 1
        return got

    # -- directory entries ----------------------------------------------
    def root_entries_iter(self):
        for slot in range(self.root_entries):
            e = self.pread(self.root_off + slot * 32, 32)
            if e[0] == 0x00:
                return
            yield slot, e

    @staticmethod
    def short_name(e: bytes) -> str:
        return (e[0:8].decode("ascii", "replace").rstrip()
                + "." + e[8:11].decode("ascii", "replace").rstrip()).rstrip(".")

    def lfn_name(self, slot: int) -> str | None:
        """Assemble the long name ending at short entry `slot`, if LFN slots
        precede it (they carry a 0x0F attribute and the short-name checksum)."""
        e = self.pread(self.root_off + slot * 32, 32)
        want_sum = e[13]
        parts: dict[int, bytes] = {}
        seq_expected = 1
        s = slot - 1
        while s >= 0:
            l = self.pread(self.root_off + s * 32, 32)
            if l[11] != 0x0F:
                return None
            seq = l[0] & 0x1F
            if l[0] & 0x40:
                seq_expected = seq  # first (last-written) segment marks the count
            if l[13] != want_sum:
                return None
            parts[seq] = bytes(l[1:11]) + bytes(l[14:26]) + bytes(l[28:32])
            if seq == 1:
                break
            s -= 1
        if len(parts) != seq_expected:
            return None
        raw = b"".join(parts[i] for i in range(1, seq_expected + 1))
        text = raw.decode("utf-16-le", "ignore").split("\x00", 1)[0]
        return text or None

    def find_entry(self, name: str) -> tuple[int, bytes] | None:
        want = name.lower()
        for slot, e in self.root_entries_iter():
            if e[11] in (0x0F, 0x08):
                continue  # LFN slot / volume label
            long = self.lfn_name(slot)
            if (long and long.lower() == want) or self.short_name(e).lower() == want:
                return slot, e
        return None

    def entry_location(self, slot: int) -> int:
        """Sector offset of the 512-byte sector holding directory entry
        `slot`. A 32-byte entry never straddles a sector (16 per sector),
        so updating an entry is exactly one sector write."""
        off = self.root_off + slot * 32
        return off - (off % SECTOR)

    def free_slot(self, need_lfn: int = 0) -> int:
        used = {slot for slot, _ in self.root_entries_iter()}
        for slot in range(need_lfn, self.root_entries):
            if all((slot - k) not in used for k in range(need_lfn + 1)):
                return slot
        raise InstallError("root directory full")

    def set_entry(self, slot: int, name: str, first: int, size: int,
                  create: bool, lfn_slots: int = 0) -> None:
        """Create or repoint a root directory entry, preserving the fixed
        FAT timestamps make-esp.py uses so repeated installs stay stable."""
        old = self.pread(self.root_off + slot * 32, 32) if create else \
            self.pread(self.root_off + slot * 32, 32)
        e = bytearray(old)
        if create:
            e = bytearray(32)
            base, _, ext = name.partition(".")
            e[0:11] = base.upper().ljust(8).encode() + ext.upper().ljust(3).encode()
            e[11] = 0x20
            struct.pack_into("<H", e, 14, 0x6D60)
            struct.pack_into("<H", e, 16, 0x5321)
            struct.pack_into("<H", e, 18, 0x5321)
            struct.pack_into("<H", e, 22, 0x6D60)
            struct.pack_into("<H", e, 24, 0x5321)
        struct.pack_into("<H", e, 20, (first >> 16) & 0xFFFF)
        struct.pack_into("<H", e, 26, first & 0xFFFF)
        struct.pack_into("<I", e, 28, size)
        self.pwrite(self.root_off + slot * 32, bytes(e))

    def write_file_content(self, clusters: list[int], data: bytes) -> None:
        """Write `data` across the given clusters (zero-padded to the cluster
        size). Caller handles the FAT links; this is content only."""
        cb = self.cluster_bytes()
        padded = data + b"\x00" * ((cb - len(data) % cb) % cb)
        for i, c in enumerate(clusters):
            chunk = padded[i * cb:(i + 1) * cb]
            self.pwrite(self.cluster_off(c), chunk)

    def link_chain(self, clusters: list[int]) -> None:
        """Write the FAT links for a chain in both copies, EOC at the end."""
        for i, c in enumerate(clusters):
            nxt = 0xFFFF if i == len(clusters) - 1 else clusters[i + 1]
            self.set_fat_entry(c, nxt)


# ------------------------------------------------------------- operations --

class Installer:
    def __init__(self, img: Fat16Image, fault: str | None, mutant: str | None):
        self.img = img
        self.fault = fault
        self.mutant = mutant
        self.op_index = 0

    def fault_before(self, op: str) -> None:
        self.op_index += 1
        print(f"C3-OP {self.op_index:02d} {op}", flush=True)
        if self.fault == f"before-{op}":
            print(f"C3-FAULT injected before {op}; dying without cleanup", flush=True)
            sys.stdout.flush()
            os._exit(137)

    def fault_mid(self, op: str, done: int, total: int) -> None:
        if self.fault == f"mid-{op}" and done == total // 2 and total >= 2:
            print(f"C3-FAULT injected mid-{op} ({done}/{total} units); dying", flush=True)
            sys.stdout.flush()
            os._exit(137)


def read_slotmap(img: Fat16Image) -> dict:
    """Parse /SLOTMAP.TXT. Missing file -> {} (factory-fresh image). Rows are
    hints: every consumer re-verifies them against image content."""
    hit = img.find_entry(SLOTMAP_NAME)
    if hit is None:
        return {}
    _, e = hit
    first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
    size = struct.unpack_from("<I", e, 28)[0]
    text = img.follow_chain(first, size).decode("ascii", "replace")
    rows: dict = {"format": 1, "slots": {}}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
        if line.startswith("slot:"):
            rows["slots"][fields["slot"]] = {k: v for k, v in fields.items() if k != "slot"}
        elif line.startswith("factory_config:"):
            rows["factory_config"] = {k: v for k, v in fields.items() if k != "factory_config:"}
        elif line.startswith("watermark:"):
            rows["watermark"] = int(fields["watermark"])
    return rows


def render_slotmap(watermark: int, factory: dict | None, slots: dict) -> bytes:
    lines = [
        "# NOVA A/B slot map (C3). Hint file: the active slot is whichever",
        "# chain /limine.conf's directory entry points at; every row here is",
        "# re-verified against image content before use.",
        f"watermark: {watermark}",
    ]
    if factory:
        lines.append("factory_config: chain=%s size=%s sha256=%s kernel=%s" % (
            factory["chain"], factory["size"], factory["sha256"], factory["kernel"]))
    for s in sorted(slots):
        r = slots[s]
        lines.append(
            "slot: {s} kernel={kernel} sequence={sequence} version={version} "
            "config_chain={config_chain} config_size={config_size} "
            "config_sha256={config_sha256} kernel_sha256={kernel_sha256}".format(s=s, **r))
    return ("\n".join(lines) + "\n").encode("ascii")


def parse_config(data: bytes) -> dict:
    """Extract the facts a boot decision needs from a limine.conf payload."""
    pins = KERNEL_PATH_RE.findall(data)
    if len(pins) != 1:
        raise InstallError(
            f"config must contain exactly one 'path: boot(1):/NUCLEUS...' line, found {len(pins)}")
    seq = SEQ_COMMENT_RE.search(data)
    ver = VER_COMMENT_RE.search(data)
    path_line = KERNEL_PATH_RE.search(data).group(0)
    pin = path_line.split(b"#", 1)[1].decode("ascii") if b"#" in path_line else None
    return {
        "kernel_file": b"/" + path_line.split(b":")[1].strip().split(b"#")[0].lstrip(b"/"),
        "blake2b": pin,
        "sequence": int(seq.group(1)) if seq else 0,
        "version": ver.group(1).decode("ascii", "replace").strip() if ver else "factory",
        "data": data,
    }


def verify_boot_target(img: Fat16Image, cfg: dict) -> None:
    """The rollback/slotmap trust gate: a chain is only usable if its pinned
    kernel exists and matches the pin. Limine re-checks the same hash at
    boot; this is the same rule enforced before we point at it."""
    hit = img.find_entry(cfg["kernel_file"].lstrip("/"))
    if hit is None:
        raise InstallError(f"config pins {cfg['kernel_file']} but the file is absent")
    _, e = hit
    first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
    size = struct.unpack_from("<I", e, 28)[0]
    kernel = img.follow_chain(first, size)
    got = blake2b_512(kernel)
    if cfg["blake2b"] is not None and got != cfg["blake2b"]:
        raise InstallError(
            f"pinned kernel {cfg['kernel_file']} does not match its blake2b pin "
            f"({got} != {cfg['blake2b']})")


def derive_slot_config(active: bytes, slot: str, kernel: bytes, seq: int, ver: str) -> bytes:
    """The new release's config: the active config with the kernel path
    re-pinned to this slot's kernel and metadata comments added. Everything
    else (timeout, serial, module_path, kernel_cmdline) is byte-identical to
    what already boots."""
    new_pin = blake2b_512(kernel).encode("ascii")
    replaced, n = KERNEL_PATH_RE.subn(
        lambda m: m.group(1) + b"boot(1):" + SLOT_FILES[slot].encode() + b"#" + new_pin,
        active)
    if n != 1:
        raise InstallError(f"expected exactly one kernel path line to re-pin, found {n}")
    header = (
        f"# nova-release-sequence: {seq}\n"
        f"# nova-release-version: {ver}\n"
        f"# nova-slot: {slot}\n").encode("ascii")
    return header + replaced


# ----------------------------------------------------------------- install --

def cmd_install(args) -> int:
    if args.fault_point and args.fault_point != "none":
        if not (args.fault_point.startswith("before-") or args.fault_point.startswith("mid-")):
            print(f"atomic_install: bad --fault-point {args.fault_point!r}", file=sys.stderr)
            return 2
    mutant = os.environ.get("NOVA_C3_MUTANT")

    manifest = nt.parse_json(Path(args.manifest).read_text(encoding="utf-8"))
    pub = nt.parse_public_key_pem(Path(args.trust_key).read_text(encoding="ascii"))
    last = nt.read_state(Path(args.state_file))
    errs = nt.verify_manifest(manifest, pub, last_accepted_sequence=last,
                              files_dir=Path(args.payload_dir))
    if errs:
        print("atomic_install: manifest rejected by the C2 verifier:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1

    bc = manifest.get("bootchain", {}).get("kernel")
    if not bc:
        print("atomic_install: manifest has no bootchain.kernel; nothing bootable "
              "to install (the kernel is what A/B slots switch)", file=sys.stderr)
        return 1
    kernel_path = Path(args.payload_dir) / bc["filename"]
    kernel = kernel_path.read_bytes()
    if hashlib.sha256(kernel).hexdigest() != bc["sha256"] or len(kernel) != bc["size"]:
        print("atomic_install: kernel payload does not match bootchain.kernel", file=sys.stderr)
        return 1

    img = Fat16Image(Path(args.image))
    inst = Installer(img, args.fault_point, mutant)
    try:
        img.check_fat_copies()
        entries = {name: img.find_entry(name)
                   for name in ("limine.conf", "EFI/BOOT/LIMINE.CONF", "NUCLEUS")}
        if entries["limine.conf"] is None:
            raise InstallError("no /limine.conf on the ESP; not a bootable NOVA image")
        root_slot, root_e = entries["limine.conf"]
        root_first = struct.unpack_from("<H", root_e, 26)[0] | (struct.unpack_from("<H", root_e, 20)[0] << 16)
        root_size = struct.unpack_from("<I", root_e, 28)[0]
        active_cfg = parse_config(img.follow_chain(root_first, root_size))

        # EFI twin: BIOS reads /limine.conf, UEFI reads /EFI/BOOT/LIMINE.CONF.
        # A crash between the two commits leaves them split; that state boots
        # on both firmwares and the next install heals it. Split is allowed;
        # a MISSING twin on a factory image is not.
        if entries["EFI/BOOT/LIMINE.CONF"] is None:
            raise InstallError("no /EFI/BOOT/LIMINE.CONF twin; refusing a UEFI-unbootable install")
        efi_slot, efi_e = entries["EFI/BOOT/LIMINE.CONF"]

        slotmap = read_slotmap(img)
        watermark = max(img.high_water(), slotmap.get("watermark", 1))
        factory_row = slotmap.get("factory_config") or {
            "chain": str(root_first), "size": str(root_size),
            "sha256": hashlib.sha256(active_cfg["data"]).hexdigest(),
            "kernel": FACTORY_KERNEL,
        }

        slots = slotmap.get("slots", {})
        # Target slot: the one whose recorded sequence is older (or absent).
        target = min(("A", "B"),
                     key=lambda s: int(slots.get(s, {}).get("sequence", 0)))
        if active_cfg["sequence"] > 0 and manifest["release"]["release_sequence"] <= active_cfg["sequence"]:
            raise InstallError(
                f"on-image sequence would move {active_cfg['sequence']} -> "
                f"{manifest['release']['release_sequence']}; refusing replay/rollback")
        if active_cfg["sequence"] > 0 and slots.get(target, {}).get("sequence") and \
                int(slots[target]["sequence"]) >= manifest["release"]["release_sequence"]:
            raise InstallError(
                f"target slot {target} holds sequence {slots[target]['sequence']}, "
                "not older than the incoming release; refusing")

        seq = manifest["release"]["release_sequence"]
        ver = manifest["release"]["version"]
        new_cfg = derive_slot_config(active_cfg["data"], target, kernel, seq, ver)
        # Sanity: the derived config must pin exactly the new kernel's digest.
        parsed = parse_config(new_cfg)
        if parsed["blake2b"] != blake2b_512(kernel):
            raise InstallError("internal: derived config does not pin the new kernel")

        cb = img.cluster_bytes()
        k_clusters = img.alloc_after(watermark, (len(kernel) + cb - 1) // cb)
        cfg_clusters = img.alloc_after(max(k_clusters), (len(new_cfg) + cb - 1) // cb)
        # Slotmap grows; give it headroom so row updates never need a new chain.
        sm_new = render_slotmap(watermark + len(k_clusters) + len(cfg_clusters) + 8,
                                factory_row, {**slots,
                                              target: {"kernel": SLOT_FILES[target],
                                                       "sequence": str(seq), "version": ver,
                                                       "config_chain": "TBD", "config_size": "TBD",
                                                       "config_sha256": "TBD",
                                                       "kernel_sha256": bc["sha256"]}})
        sm_clusters = img.alloc_after(max(cfg_clusters), (len(sm_new) + cb - 1) // cb)
        watermark_next = max(sm_clusters)

        # -- staged writes: everything below touches fresh clusters only ----
        inst.fault_before("stage_kernel_content")
        for i, c in enumerate(k_clusters):
            img.pwrite(img.cluster_off(c),
                       (kernel + b"\x00" * cb)[i * cb:(i + 1) * cb])
            img.flush()
            inst.fault_mid("stage_kernel_content", i + 1, len(k_clusters))

        inst.fault_before("stage_kernel_fat")
        img.link_chain(k_clusters)

        inst.fault_before("stage_kernel_dirent")
        hit = img.find_entry(SLOT_FILES[target].lstrip("/"))
        if hit is None:
            slot_e = img.free_slot()
            img.set_entry(slot_e, SLOT_FILES[target].lstrip("/"),
                          k_clusters[0], len(kernel), create=True)
        else:
            slot_e = hit[0]
            img.set_entry(slot_e, SLOT_FILES[target].lstrip("/"),
                          k_clusters[0], len(kernel), create=False)

        if mutant == "skip-config-stage":
            # SABOTAGE GATE ONLY: commit with a config chain that was never
            # written. scripts/test-atomic-install.sh requires this image to
            # FAIL to boot.
            cfg_clusters = img.alloc_after(max(k_clusters), 1)
        else:
            inst.fault_before("stage_config_content")
            for i, c in enumerate(cfg_clusters):
                img.pwrite(img.cluster_off(c),
                           (new_cfg + b"\x00" * cb)[i * cb:(i + 1) * cb])
                img.flush()
                inst.fault_mid("stage_config_content", i + 1, len(cfg_clusters))
            inst.fault_before("stage_config_fat")
            img.link_chain(cfg_clusters)

            # Read-back the staged config before pointing at it.
            back = img.follow_chain(cfg_clusters[0], len(new_cfg))
            if back != new_cfg:
                raise InstallError("staged config read-back mismatch; refusing to commit")

        inst.fault_before("stage_slotmap_content")
        sm_final = render_slotmap(watermark_next, factory_row,
                                  {**slots, target: {
                                      "kernel": SLOT_FILES[target],
                                      "sequence": str(seq), "version": ver,
                                      "config_chain": str(cfg_clusters[0]),
                                      "config_size": str(len(new_cfg)),
                                      "config_sha256": hashlib.sha256(new_cfg).hexdigest(),
                                      "kernel_sha256": bc["sha256"]}})
        for i, c in enumerate(sm_clusters):
            img.pwrite(img.cluster_off(c),
                       (sm_final + b"\x00" * cb)[i * cb:(i + 1) * cb])
        inst.fault_before("stage_slotmap_fat")
        img.link_chain(sm_clusters)

        inst.fault_before("stage_slotmap_dirent")
        sm_hit = img.find_entry(SLOTMAP_NAME)
        if sm_hit is None:
            img.set_entry(img.free_slot(), SLOTMAP_NAME,
                          sm_clusters[0], len(sm_final), create=True)
        else:
            img.set_entry(sm_hit[0], SLOTMAP_NAME,
                          sm_clusters[0], len(sm_final), create=False)

        inst.fault_before("sync_stage")
        img.flush()

        # -- the commit: two single-sector directory writes ------------------
        def commit(slot_idx: int, name_for_print: str) -> None:
            sector_off = img.entry_location(slot_idx)
            sector = bytearray(img.pread(sector_off, SECTOR))
            rel = (img.root_off + slot_idx * 32) - sector_off
            e = bytearray(sector[rel:rel + 32])
            struct.pack_into("<H", e, 20, (cfg_clusters[0] >> 16) & 0xFFFF)
            struct.pack_into("<H", e, 26, cfg_clusters[0] & 0xFFFF)
            struct.pack_into("<I", e, 28, len(new_cfg))
            sector[rel:rel + 32] = bytes(e)
            img.pwrite(sector_off, bytes(sector))

        inst.fault_before("commit_root_ptr")
        commit(root_slot, "/limine.conf")
        inst.fault_before("commit_efi_ptr")
        commit(efi_slot, "/EFI/BOOT/LIMINE.CONF")

        inst.fault_before("sync_commit")
        img.flush()

        # Post-commit read-back through the directory entry: proof the
        # pointer really resolves to the staged config.
        reread_slot, reread_e = img.find_entry("limine.conf")
        reread_first = struct.unpack_from("<H", reread_e, 26)[0] | \
            (struct.unpack_from("<H", reread_e, 20)[0] << 16)
        reread_size = struct.unpack_from("<I", reread_e, 28)[0]
        if (mutant != "skip-config-stage" and
                img.follow_chain(reread_first, reread_size) != new_cfg):
            raise InstallError("post-commit read-back mismatch; the commit did not stick")

        inst.fault_before("write_state")
        nt.write_state(Path(args.state_file), seq)
        img.close()
        print(f"atomic_install: committed release {ver} (sequence {seq}) "
              f"to slot {target}: config chain {cfg_clusters[0]}, kernel "
              f"{SLOT_FILES[target]} ({len(kernel)} B, blake2b {parsed['blake2b'][:16]}...)")
        return 0
    except InstallError as e:
        print(f"atomic_install: {e}", file=sys.stderr)
        return 1


# ----------------------------------------------------------- status/rollback --

def active_state(img: Fat16Image) -> dict:
    hit = img.find_entry("limine.conf")
    if hit is None:
        raise InstallError("no /limine.conf on the ESP")
    _, e = hit
    first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
    size = struct.unpack_from("<I", e, 28)[0]
    cfg = parse_config(img.follow_chain(first, size))
    efi = img.find_entry("EFI/BOOT/LIMINE.CONF")
    efi_first = None
    if efi is not None:
        ee = efi[1]
        efi_first = struct.unpack_from("<H", ee, 26)[0] | (struct.unpack_from("<H", ee, 20)[0] << 16)
    slotmap = read_slotmap(img)
    role = "factory"
    if cfg["sequence"] > 0:
        role = f"slot-A/B lookup"
        for s, row in slotmap.get("slots", {}).items():
            if row.get("config_sha256") == hashlib.sha256(cfg["data"]).hexdigest():
                role = f"slot {s}"
                break
    return {
        "active": role, "sequence": cfg["sequence"], "version": cfg["version"],
        "kernel_file": cfg["kernel_file"].decode("ascii", "replace"),
        "blake2b": cfg["blake2b"], "config_chain": first, "config_size": size,
        "efi_chain": efi_first, "slotmap": slotmap,
        "watermark": max(img.high_water(), slotmap.get("watermark", 1)),
    }


def cmd_status(args) -> int:
    img = Fat16Image(Path(args.image))
    st = active_state(img)
    img.close()
    print(f"active: {st['active']} sequence={st['sequence']} version={st['version']}")
    print(f"kernel: {st['kernel_file']} blake2b={st['blake2b']}")
    print(f"config: chain={st['config_chain']} size={st['config_size']} "
          f"efi_chain={st['efi_chain']} {'(SPLIT from root - crash between commits; next install heals)' if st['efi_chain'] not in (None, st['config_chain']) else ''}")
    sm = st["slotmap"]
    if sm.get("factory_config"):
        f = sm["factory_config"]
        print(f"factory: chain={f['chain']} sha256={f['sha256'][:16]}... kernel={f['kernel']}")
    for s, row in sorted(sm.get("slots", {}).items()):
        print(f"slot {s}: kernel={row['kernel']} sequence={row['sequence']} "
              f"version={row['version']} config_chain={row['config_chain']}")
    print(f"watermark: {st['watermark']}")
    return 0


def cmd_rollback(args) -> int:
    img = Fat16Image(Path(args.image))
    try:
        st = active_state(img)
        cur_seq = st["sequence"]
        cur_chain = st["config_chain"]
        slotmap = st["slotmap"]
        candidates = []
        if slotmap.get("factory_config") and cur_seq > 0:
            candidates.append(("factory", slotmap["factory_config"], 0))
        for s, row in slotmap.get("slots", {}).items():
            rseq = int(row.get("sequence", 0))
            if rseq < cur_seq:
                candidates.append((f"slot {s}", row, rseq))
        if not candidates:
            print("atomic_install: nothing to roll back to (already at the oldest "
                  "recorded release); refusing", file=sys.stderr)
            return 1
        # Highest sequence below the current one = the previous release.
        name, row, rseq = max(candidates, key=lambda c: c[2])
        first = int(row["config_chain"])
        size = int(row["config_size"])
        if first == cur_chain:
            print("atomic_install: rollback target is already active; refusing", file=sys.stderr)
            return 1
        # Trust gate: the recorded chain must still exist and hash to the
        # recorded content, and its pinned kernel must exist and match.
        data = img.follow_chain(first, size)
        if hashlib.sha256(data).hexdigest() != row["config_sha256"]:
            print(f"atomic_install: rollback target {name} chain content does not "
                  "match the slotmap record; refusing (fail closed)", file=sys.stderr)
            return 1
        cfg = parse_config(data)
        verify_boot_target(img, cfg)

        # Same commit primitive as install: one sector per directory entry.
        root_slot, _ = img.find_entry("limine.conf")
        efi_slot, _ = img.find_entry("EFI/BOOT/LIMINE.CONF")

        def commit(slot_idx: int) -> None:
            sector_off = img.entry_location(slot_idx)
            sector = bytearray(img.pread(sector_off, SECTOR))
            rel = (img.root_off + slot_idx * 32) - sector_off
            e = bytearray(sector[rel:rel + 32])
            struct.pack_into("<H", e, 20, (first >> 16) & 0xFFFF)
            struct.pack_into("<H", e, 26, first & 0xFFFF)
            struct.pack_into("<I", e, 28, size)
            sector[rel:rel + 32] = bytes(e)
            img.pwrite(sector_off, bytes(sector))

        commit(root_slot)
        commit(efi_slot)
        img.flush()
        img.close()
        print(f"atomic_install: rolled back to {name} "
              f"(sequence {rseq}, version {cfg['version']}); replay state NOT "
              "decremented - a re-install still needs a newer signed release")
        return 0
    except InstallError as e:
        print(f"atomic_install: {e}", file=sys.stderr)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="C3 atomic A/B installer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install")
    p.add_argument("--image", required=True, type=Path)
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--payload-dir", required=True, type=Path)
    p.add_argument("--trust-key", required=True, type=Path)
    p.add_argument("--state-file", required=True, type=Path)
    p.add_argument("--fault-point", default="none",
                   help="before-<op> | mid-<op>; kills the process at that point")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("status")
    p.add_argument("--image", required=True, type=Path)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("rollback")
    p.add_argument("--image", required=True, type=Path)
    p.set_defaults(fn=cmd_rollback)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
