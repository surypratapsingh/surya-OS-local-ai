# NOVA — hardware matrix (work order W3)

> Work order: `docs/work-orders.md` §W3. **M0 is not ✅ until this table exists
> and every row passes.** A row that could not be tested is recorded as
> UNTESTED below, never omitted. No row is ever deleted: a FAIL stays in the
> table with its evidence — that is the finding.
>
> Row rule: a row without committed evidence is not a PASS. Where a row could
> not be run yet, the machine line stays and the result reads UNTESTED.

Status key: **PASS** · **FAIL** · **UNTESTED**.
Evidence is either a committed capture under `docs/logs/` (serial log or
photograph) or nothing.

---

## 1. Writing the stick

Everything below operates on `build/nova.hdd`, a 40 MiB BIOS **and** UEFI
bootable image. Building it requires the gate to have run first:

```bash
export PATH="$HOME/.cargo/bin:$PATH"      # rustup, if not already on PATH
bash scripts/build-disk.sh release        # -> build/nova.hdd
bash scripts/check.sh                     # 8-stage gate; the disk stages must pass
```

The same steps on Linux/WSL with `make`: `cd kernel && make disk && make check`.
`check.sh` stage 1 verifies every third-party binary against the committed pins
in `tools/manifest/bootchain.sha256` (`scripts/trust.sh verify`), and stage 5
verifies the finished image structurally with `tools/verify-disk.py` (written
from UEFI 2.10 §5.2–§5.3). Do not write a stick that did not pass.

Then write the image to a pendrive — **64 MB or larger, and everything already
on the stick is destroyed**:

- **Windows:** [Rufus](https://rufus.ie) → select `nova.hdd` → GPT/UEFI (or
  MBR/BIOS; the image carries both paths).
- **Linux/macOS:** `dd if=build/nova.hdd of=/dev/sdX bs=4M status=progress` —
  check the device with `lsblk` first; `of=` with the wrong device destroys
  that disk with no undo.

## 2. Capturing evidence for a row

The kernel's boot report goes to serial: COM1 (base port `0x3F8`), 115200 baud,
8N1 — set in `kernel/src/serial.rs`. Two accepted evidence forms per the work
order ("a serial capture or photograph"):

**Serial capture.** With a DB9 port or a motherboard serial header, attach a
USB serial adapter and record the boot (115200 8N1), e.g. with `py -3` +
`pyserial` (not used elsewhere in this repo; any terminal program — Tera Term,
`screen /dev/ttyUSB0 115200` — is fine). The lines that make the row are
`NOVA_BOOT_OK`, the console banner, and the `nova>` prompt.

**Photograph.** No serial port is required to PASS. Photograph the screen after
boot; the frame must show the NOVA boot banner and the `nova>` prompt.
Name evidence files `docs/logs/hw-YYYY-MM-DD-<machine-slug>.jpg` (or `.log`)
and commit them before marking the row PASS.

**What a PASS means.** (1) the machine's firmware booted the stick (UEFI from
the ESP, or BIOS via the MBR); (2) Limine loaded and handed off to Nucleus;
(3) the boot report appeared; (4) the shell prompt appeared and accepted an
interactive keypress where a keyboard was attached.

## 3. Result matrix

| # | Machine | Firmware | Mode | Secure Boot | Result | Evidence |
|---|---------|----------|------|-------------|--------|----------|
| 1 | QEMU x86_64 (dev host, Windows; QEMU version not recorded in the log) | SeaBIOS | BIOS | n/a (SeaBIOS has none) | PASS | `docs/logs/w2-check-full.log` stage 8, `SeaBIOS/hdd` + `SeaBIOS/selftest` |
| 2 | QEMU x86_64 (dev host, Windows; QEMU version not recorded in the log) | OVMF (edk2) | UEFI | off — no keys enrolled in the test firmware | PASS | `docs/logs/w2-check-full.log` stage 8, `OVMF/hdd` + `OVMF/selftest` |
| 3 | `<machine model>` | vendor firmware | BIOS | `<state>` | UNTESTED | — |
| 4 | `<machine model>` | vendor firmware | UEFI | `<state>` | UNTESTED | — |
| 5 | `<machine model>` | vendor firmware | UEFI | on, vendor keys | UNTESTED | — |

The QEMU serial tails those rows cite, copied exactly from
`docs/logs/w2-check-full.log` stage 8 (commit `46771d6`, run 2026-09-24):

```
--- SeaBIOS/hdd: exit=124 serial tail:
      memmap:    base=0x00000000fffc0000 len=0x40000 reserved
      memmap:    base=0x000000fd00000000 len=0x300000000 reserved
    NOVA_BOOT_OK
    keyboard:    PS/2 poller ready
    NOVA Nucleus K2 console. Type `help`.
    nova> PASS (SeaBIOS/hdd)
--- OVMF/hdd: exit=124 serial tail:
      memmap:    base=0x00000000fffc0000 len=0x40000 reserved
      memmap:    base=0x000000fd00000000 len=0x300000000 reserved
    NOVA_BOOT_OK
    keyboard:    PS/2 poller ready
    NOVA Nucleus K2 console. Type `help`.
    nova> PASS (OVMF/hdd)
--- SeaBIOS/selftest: exit=33 serial tail:
      memmap:    base=0x00000000fffc0000 len=0x40000 reserved
      memmap:    base=0x000000fd00000000 len=0x300000000 reserved
    NOVA_BOOT_OK
    keyboard:    PS/2 poller ready
    NOVA Nucleus K2 console. Type `help`.
    nova> NOVA_SELFTEST_OK
PASS (SeaBIOS/selftest)
--- OVMF/selftest: exit=33 serial tail:
      memmap:    base=0x00000000fffc0000 len=0x40000 reserved
      memmap:    base=0x000000fd00000000 len=0x300000000 reserved
    NOVA_BOOT_OK
    keyboard:    PS/2 poller ready
    NOVA Nucleus K2 console. Type `help`.
    nova> NOVA_SELFTEST_OK
PASS (OVMF/selftest)
```

(`exit=124` is the expected timeout-kill of the interactive console case;
`exit=33` is the selftest image's clean `isa-debug-exit`. The earlier
generation of these rows, from `docs/logs/w1-check-pass.txt` commit `c910b7e`,
2026-09-22, shows the same four cases passing.)

Known caveat, recorded honestly: rows 1–2 are evidenced by the committed host
logs above. Whether the same cases reproduce in this repository's CI is
**UNTESTED** as of this draft and is not claimed either way.

## 4. Real-machine rows — how to fill them

Rows 3–5 are placeholders because no real machine has been booted yet. To turn
one into a real row:

1. Build and gate-check the image (§1), write the stick (§1), boot the machine
   from USB (BIOS machines: boot-menu USB/HDD entry; UEFI machines: boot-menu
   USB entry — disable Secure Boot **only if** you record that fact in the row;
   a Secure-Boot-on result is its own row, PASS or FAIL).
2. Capture evidence per §2 and commit it under `docs/logs/`.
3. Replace the placeholder row with: machine model, firmware vendor/version,
   mode, Secure Boot state, result, evidence path.
4. Record quirks under §5 — PS/2 vs USB keyboard behaviour, screen resolution,
   boot delay, anything the next person will want to know.

W3's gate is rows for **at least two real machines** plus the two QEMU rows.
Rows 3–5 above are where those results go.

## 5. Machine notes

(record per-machine quirks here as rows are filled)

- (none yet)
