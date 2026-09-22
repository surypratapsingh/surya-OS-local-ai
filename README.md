# NOVA — a local-first AI OS you own

**NOVA** is an operating system project with one rule: *it serves the owner, and only the
owner.* One person owns the machine, the models, and the data. Everything runs locally;
nothing phones home; new knowledge and abilities arrive on a **signed pendrive** — carried in
by you, verified before install.

The full refined vision lives in [`docs/plan-v2.md`](docs/plan-v2.md).

## What works today

| Piece | What it is | Status |
|---|---|---|
| **Nucleus** (`kernel/`) | From-scratch x86-64 kernel in Rust (`no_std`), booted by Limine v12.9.0. K1 = boots, draws the NOVA logo, logs `NOVA_BOOT_OK` on serial. | ✅ **boots** |
| **Pendrive image** (`build/nova.hdd`) | One raw disk image that boots **both** legacy BIOS and UEFI from any pendrive. Built by a pure-Python tool — no mtools/xorriso needed, works on Windows and Linux. | ✅ **verified** |
| **Verifier** (`tools/verify-disk.py`) | Parses the image back the way firmware would: GPT CRCs, FAT16, directory walk, byte-exact file round-trip. | ✅ **42 checks** |
| **NovaCore AI** (planned `eyes/` → `novacore/`) | Camera → emotion → voice → journal; then skills, study pipeline, case memory. | ⏳ next |

## Quick start — build the stick

From the repo root (Git Bash on Windows, any shell on Linux/WSL):

```
export PATH="$HOME/.cargo/bin:$PATH"     # rustup, if not already on PATH
bash scripts/build-disk.sh release       # → build/nova.hdd  (BIOS + UEFI bootable)
bash scripts/check.sh                    # structural verification + QEMU boot tests
```

With `make` (Linux/WSL), the same via `cd kernel && make disk && make check`.

**Write it to a pendrive** (64 MB or bigger — everything is destroyed on the stick):

- Windows: [Rufus](https://rufus.ie) → select `nova.hdd` → GPT/UEFI (or MBR/BIOS).
- Linux/macOS: `dd if=build/nova.hdd of=/dev/sdX bs=4M status=progress` (careful with X!).

Plug it into any x86-64 machine, boot from USB, and NOVA's own kernel takes over.

**Boot tests in QEMU:** install QEMU (`winget install SoftwareFreedomConservancy.QEMU` on
Windows, `sudo apt install qemu-system-x86` on Debian/Ubuntu), then `make check` runs the
image under SeaBIOS and OVMF headlessly and asserts `NOVA_BOOT_OK` on serial + a clean
exit. `make run` / `make run-uefi` / `make run-disk` open a window to watch it boot.

## Repo layout

```
nova/
├── kernel/            # Nucleus (Track A) — Rust no_std x86-64 kernel
│   ├── src/           #   limine.rs, serial.rs, framebuffer.rs, font.rs, draw.rs,
│   │                  #   panic.rs, qemu_exit.rs, main.rs
│   ├── limine.conf    #   boot menu entry (kernel at /NUCLEUS)
│   ├── linker.ld
│   ├── Makefile       #   build / iso / disk / run / run-uefi / run-disk / check
│   └── scripts/       #   build-iso.sh, run-qemu.sh, test-boot.sh, fetch-limine.sh
├── scripts/           # build-disk.sh (pendrive image), check.sh (full verify)
├── tools/             # make-esp.py (GPT+FAT16 builder), verify-disk.py
├── docs/              # plan-v2, vision, architecture, privacy, roadmap
└── .freebuff/ref/     # vendored Limine v12.9.0 binaries + protocol spec
```

## How NOVA serves you (the design, in one scene)

You want to study maths. You carry a `maths-pack` in on the stick — PDFs, lecture videos,
photos of your notes. While you sleep, NOVA indexes it: text extracted, video transcribed
locally, handwriting OCR'd, every page turned into searchable vectors on your disk.

Next morning: *"Study chapter 3."* It teaches the chapter, draws the graphs on screen,
quizzes you, and remembers which problems you solved — so next month a similar problem
replays your own method instantly instead of re-deriving it. When it hits something it
doesn't have — *"no more information on matrices beyond class 10"* — it doesn't reach for
the internet. It writes a **demand list entry**: exactly what to download, from where, step
by step. You fill the list on any internet machine, carry it back on the stick, and it
installs itself — verified, offline, yours.

Want music? A game? A module is demanded, shuttled, installed — same protocol. The model
running the show is small and fast; when a task genuinely needs more, it tells you which
bigger model to bring it, and otherwise keeps the whole machine for you.

## Why it works this way

- **Small code, big intelligence.** The OS code stays tiny (KBs–MBs). Intelligence is
  *data* — model weights, indexes, skill modules — swapped in like cassettes.
- **Privacy is structural, not a setting.** Camera frames live in RAM only. The journal
  stores `{timestamp, emotion, confidence}` text — nothing else. Pendrive packages are
  ed25519-signed and verified before anything is applied.
- **Minimal base, maximal AI.** The daily driver (M1) is a ~300 MB minimal-Linux image with
  no daemons and no store, so the AI gets ~90% of the machine. Nucleus grows underneath
  (K2→K6) until it can replace Linux entirely.

## Milestones

- **Kernel (K):** K1 boot ✅ · K1.5 pendrive ✅ · K2 console · K3 memory+FAT32 ·
  K4 userspace · K5 USB/audio+native AI · K6 NovaCore on Nucleus.
- **AI core (E):** E0 eyes · E1 ears+brain · E2 shuttle · E3 thrift · E4 case memory ·
  E5 shell.
- **Owner (M):** M0 the stick ✅ · M1 daily driver · M2 study pipeline · M3 module manager ·
  M4 self-improvement · M5 full circle.

See [`docs/roadmap.md`](docs/roadmap.md) for the full ladder and
[`docs/vision.md`](docs/vision.md), [`docs/architecture.md`](docs/architecture.md),
[`docs/privacy.md`](docs/privacy.md) for the design story.
