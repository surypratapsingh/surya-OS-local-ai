#!/usr/bin/env python3
"""External oracle verification of a NOVA boot image (work order W2, item 2).

Runs three tools that did NOT come from this repo — sgdisk (gdisk package),
fsck.fat (dosfstools), mdir (mtools) — against build/nova.hdd. They are the
oracles: they share no code with tools/make-esp.py or tools/verify-disk.py,
so a misconception builder and verifier have in common is caught by them.

A missing tool prints a loud SKIP naming it and is counted in the summary —
a skip is never recorded as a pass. Note: the mdir listing parser below was
written without access to the tool (not installed on the build machine);
its first real execution is in CI. Until then the mdir comparison is
best-effort: an unparsable listing FAILS, it can never silently pass.

Exit 1 on any oracle failure; exit 0 with skips if all tools are absent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_disk as vd  # noqa: E402

MISSING_INSTALL_HINT = {
    "sgdisk": "apt-get install gdisk",
    "fsck.fat": "apt-get install dosfstools",
    "mdir": "apt-get install mtools",
}

failures: list[str] = []
skips: list[str] = []


def run_tool(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def sgdisk_check(img: Path) -> None:
    if not shutil.which("sgdisk"):
        skips.append(f"sgdisk not found (install: {MISSING_INSTALL_HINT['sgdisk']})")
        print("  SKIP  sgdisk not found — GPT oracle NOT run (never silently passed)")
        return
    rc, out = run_tool(["sgdisk", "--verify", str(img)])
    print(f"--- sgdisk --verify (exit {rc})")
    for line in out.splitlines():
        print(f"    {line}")
    if rc != 0:
        failures.append(f"sgdisk --verify exited {rc}")
        print("  FAIL  sgdisk --verify")
    else:
        print("  ok    sgdisk --verify reports no problems")


def fsck_check(img: Path) -> None:
    if not shutil.which("fsck.fat"):
        skips.append(f"fsck.fat not found (install: {MISSING_INSTALL_HINT['fsck.fat']})")
        print("  SKIP  fsck.fat not found — FAT oracle NOT run (never silently passed)")
        return
    # Hand fsck.fat the ESP partition, not the whole disk: it checks
    # filesystems, not partition tables.
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
        esp_path = Path(tmp.name)
    try:
        img_bytes = img.read_bytes()
        esp = img_bytes[vd.PART_START_LBA * vd.SECTOR:vd.BACKUP_ENTRIES_LBA * vd.SECTOR]
        esp_path.write_bytes(esp)
        rc, out = run_tool(["fsck.fat", "-n", "-v", str(esp_path)])
        print(f"--- fsck.fat -n (exit {rc})")
        for line in out.splitlines()[:15]:
            print(f"    {line}")
        if rc != 0:
            failures.append(f"fsck.fat -n exited {rc}")
            print("  FAIL  fsck.fat -n (filesystem errors)")
        else:
            print("  ok    fsck.fat -n finds no filesystem errors")
    finally:
        esp_path.unlink(missing_ok=True)


def parse_mdir(text: str) -> dict[str, int | None]:
    """Parse a default-mode mdir listing into {NAME_UPPER: size_or_None}.

    Handles LFN lines and their indented 8.3 duplicates by stripping the
    trailing timestamp and taking the last numeric token as the size.
    """
    entries: dict[str, int | None] = {}
    for line in text.splitlines():
        m = re.search(r"\s{2,}\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s*$", line)
        if not m:
            continue
        body = line[:m.start()].strip()
        mm = re.match(r"^(.+?)\s{2,}(<DIR>|\d+)$", body)
        if not mm:
            continue
        name = mm.group(1).replace(" ", "").upper()
        if not name or name in (".", ".."):
            continue
        entries[name] = None if mm.group(2) == "<DIR>" else int(mm.group(2))
    return entries


def mdir_check(img: Path, expect: vd.Expect) -> None:
    if not shutil.which("mdir"):
        skips.append(f"mdir not found (install: {MISSING_INSTALL_HINT['mdir']})")
        print("  SKIP  mdir not found — directory oracle NOT run (never silently passed)")
        return
    offset = vd.PART_START_LBA * vd.SECTOR
    targets = {
        "::/": {
            "EFI": None,
            "LIMINE": None,
            "NUCLEUS": len(expect.kernel),
            "LIMINE.CONF": len(expect.conf),
        },
        "::/EFI/BOOT": {
            "BOOTX64.EFI": len(expect.bootx64),
            "BOOTIA32.EFI": len(expect.bootia32),
            "LIMINE.CONF": len(expect.conf),
        },
        "::/LIMINE": {
            "LIMINE-BIOS.SYS": len(expect.limine_sys),
        },
    }
    for dirpath, wanted in targets.items():
        rc, out = run_tool(["mdir", "-i", f"{img}@@{offset}", dirpath])
        print(f"--- mdir {dirpath} (exit {rc})")
        for line in out.splitlines()[:12]:
            print(f"    {line}")
        if rc != 0:
            failures.append(f"mdir {dirpath} exited {rc}")
            print(f"  FAIL  mdir {dirpath}")
            continue
        found = parse_mdir(out)
        if not found:
            failures.append(f"mdir {dirpath}: listing unparsable")
            print(f"  FAIL  mdir {dirpath}: listing unparsable (parser needs fixing)")
            continue
        for name, size in wanted.items():
            if name not in found:
                failures.append(f"mdir {dirpath}: missing {name}")
                print(f"  FAIL  mdir {dirpath}: {name} missing from listing")
            elif size is not None and found[name] != size:
                failures.append(f"mdir {dirpath}: {name} size {found[name]} != {size}")
                print(f"  FAIL  mdir {dirpath}: {name} size {found[name]} != build input {size}")
            else:
                print(f"  ok    mdir {dirpath}: {name} present"
                      + (f" ({size} bytes, matches build input)" if size else " (dir)"))
        extras = set(found) - set(wanted)
        if extras:
            print(f"  note  mdir {dirpath}: extra listing entries ignored "
                  f"(8.3 duplicates of LFN names): {sorted(extras)}")


def main() -> int:
    img = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/nova.hdd")
    kernel_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("kernel")
    limine_bin = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".freebuff/ref/limine/limine-binary")
    elf = (Path(sys.argv[4]) if len(sys.argv) > 4 else
           kernel_dir / "target/x86_64-unknown-none/release/nucleus")

    expect = vd.load_expectations(kernel_dir, limine_bin, kernel_elf=elf)
    print(f"oracle-disk: external oracles against {img}")
    sgdisk_check(img)
    fsck_check(img)
    mdir_check(img, expect)
    print()
    if failures:
        print(f"oracle-disk: {len(failures)} oracle check(s) FAILED")
        return 1
    if skips:
        print(f"oracle-disk: 0 failed, {len(skips)} SKIPPED (tools absent): {skips}")
        print("oracle-disk: skips are NOT passes — install the tools for full coverage")
        return 0
    print("oracle-disk: all oracle checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
