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

import importlib.util  # noqa: E402

# Load by path: the module file is 'verify-disk.py' (hyphenated), which the
# plain 'import' statement cannot name.
_spec = importlib.util.spec_from_file_location(
    "verify_disk", Path(__file__).resolve().parent / "verify-disk.py")
vd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vd)

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
        # Full output: fsck names the exact defect it found, and truncating
        # (the 15-line cap before CI-1) hides the diagnosis while keeping the
        # banner. 40 lines is more than fsck.fat emits for a clean small FS.
        for line in out.splitlines()[:40]:
            print(f"    {line}")
        if rc != 0:
            failures.append(f"fsck.fat -n exited {rc}")
            print("  FAIL  fsck.fat -n (filesystem errors — full output above)")
        else:
            print("  ok    fsck.fat -n finds no filesystem errors")
    finally:
        esp_path.unlink(missing_ok=True)


def parse_mdir(text: str) -> dict[str, int | None]:
    """Parse a default-mode mdir listing into {NAME_UPPER: size_or_None}.

    Format below is taken verbatim from the first real mdir run in CI
    (run 35997229547, mtools on ubuntu-latest), not invented:

        NUCLEUS          47384 2021-09-01  13:43        <- ONE space before date
        EFI          <DIR>     2021-09-01  13:43
        LIMINE~1 CON       248 2021-09-01  13:43  limine.conf
        LIMINE~1 SYS    330888 2021-09-01  13:43  limine-bios.sys
                         <DIR>     2021-09-01  13:43        <- '.'/'..': blank name

    mtools prints the 8.3 name in a fixed 11-character column: chars 0..7
    are the base name, chars 8..10 the extension (dotless, padded with
    spaces; both may be blank for '.'/'..'). The long-file name, when the
    entry has one, trails the datetime. So the dotted name is reconstructed
    from the fixed field, and the LFN name is added as a second key.
    """
    entries: dict[str, int | None] = {}
    for line in text.splitlines():
        m = re.search(r"\s(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s*(.*)$", line)
        if not m:
            continue
        long_name = m.group(2).strip()
        # mdir indents every listing line (observed: 5 spaces); strip the
        # indent first, THEN the fixed 12-char 8.3 column is aligned: base
        # 0..7, a space at 8, extension 9..11 ('LIMINE~1 CON', 'BOOTX64  EFI').
        # Dot entries ('.'/'..') print NO name column at all — the remainder
        # is just '<DIR>' — so they are skipped before any slicing.
        work = line[: m.start()].strip()
        if not work or work == "<DIR>":
            continue
        base = work[0:8].strip()
        ext = work[9:12].strip()
        name = f"{base}.{ext}" if base and ext else base
        if not name or name in (".", ".."):
            continue
        size_tok = work[12:].strip()
        size = None if size_tok == "<DIR>" else int(size_tok)
        entries[name.upper()] = size
        if long_name:
            entries[long_name.upper()] = size
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
