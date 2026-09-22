# NOVA — Roadmap

Status marks: ✅ done (cites committed evidence under `docs/logs/`) · 🔨 in progress · ⏳ planned
Evidence set: `docs/logs/w1-check-pass.txt` (full 6-stage gate),
`docs/logs/w1-qemu-typing.log` (BIOS boot + interactive shell session),
`docs/logs/w1-font-mutation.log` (deliberate glyph corruption caught by the runtime check).
Ladders: **K** = Nucleus kernel · **E** = NovaEyes/NovaCore AI core · **M** = owner-facing
milestones (what you can hold and use). K and E are how we get there; M is the product.

## Track A — Nucleus kernel (`kernel/`)

- ✅ **K1 — It boots.** Limine v12.9.0 (UEFI+BIOS) loads a Rust `no_std` ELF at the canonical
  higher-half base; boot report on serial (`COM1`, ends in `NOVA_BOOT_OK`); NOVA logo drawn to
  the framebuffer with the embedded 8×8 font; clean CI exit under QEMU (`isa-debug-exit`, 33).
  Evidence: `docs/logs/w1-check-pass.txt` (both firmware paths, regular + selftest).
- ✅ **K1.5 — The pendrive.** `build/nova.hdd`: GPT + FAT16 ESP + BIOS-boot partition, built by
  a pure-Python tool (`tools/make-esp.py`), verified structurally by `tools/verify-disk.py`
  (44 checks, byte-exact file round-trip), BIOS stages installed by `limine bios-install`.
  Boots SeaBIOS and UEFI from one image. `make disk` / `make check`.
  Evidence: `docs/logs/w1-check-pass.txt` stages 3–6.
- ✅ **K2 — Console.** PS/2 keyboard polling (ports 0x60/0x64) with correct controller
  translation handling (the double-translation bug found and fixed via a QEMU monitor probe),
  scancode set 1 → ASCII with Shift/CapsLock, 95-glyph font verified byte-for-byte against
  the reference by `tests/test_font_ref.py` (host) plus a structural runtime check that was
  proven to fire by deliberately corrupting a glyph (`docs/logs/w1-font-mutation.log`),
  format-correct 16/24/32-bpp framebuffer writes, text console with scrolling + serial mirror,
  tiny shell (`help`, `clear`, `mem`, `ver`, `reboot`, `halt`). Interactive typing verified in
  QEMU via monitor `sendkey` (`docs/logs/w1-qemu-typing.log`). CI selftest image (`novatest`
  cmdline → clean exit 33) plus regular-image prompt check. **Caveat:** the work-order K2 gate
  — every exception vector deliberately triggered by a test — is NOT yet met; the IDT has
  handlers registered for 19 vectors but none has been fired on purpose. That remains open
  work under the K2 label, not a completed claim.
  Note: `qemu_exit` remains compiled into the binary but is reachable only when the
  bootloader passes the `novatest` command line (`main.rs`), so real-hardware boots idle at
  the shell instead of exiting; a cargo-feature gate is planned with the K2 exception-test
  work.
- ⏳ **K3 — Memory + files.** 4-level paging, physical frame allocator from the Limine memory
  map, kernel heap (buddy+slab), FAT32 read-only driver (first in-kernel pendrive read),
  CMOS RTC (real time for the AI core).
- ⏳ **K4 — Userspace.** ELF64 loader, syscalls (`read`/`write`/`exit`/`spawn`), cooperative
  then preemptive scheduling (APIC timer), `novad` init + user shell.
- ⏳ **K5 — Devices + native AI.** xHCI USB stack, UVC webcam class driver, Intel HDA audio
  out; port llama.cpp + whisper.cpp + Piper (C/C++ on the Nucleus libc shim + VFS).
- ⏳ **K6 — NovaCore on Nucleus.** The AI brain runs on the owner's own kernel. NOVA is whole.

## Track B — NovaEyes → NovaCore AI core (`eyes/` → `novacore/`)

- ⏳ **E0 — Eyes open.** Camera → YuNet face detection → HSEmotion 8-emotion classification →
  time-aware spoken greeting (Piper) → local text journal. Tier-aware performance. Frames
  never touch disk.
- ⏳ **E1 — Ears + brain.** Wake word, whisper.cpp STT, Qwen-class Q4 model via llama.cpp,
  tool-calling planner bound to the skill library.
- ⏳ **E2 — Shuttle.** ed25519-signed pendrive packages + the demand list (see plan-v2);
  packer runs on any net machine the owner controls, verifier runs locally.
- ⏳ **E3 — Thrift.** Resource governor: battery/thermal awareness, model cascade, idle-time
  deferral of heavy work (indexing, transcription).
- ⏳ **E4 — Case memory + Metamorph.** Solved-problem replay (semantic cache); nightly
  reflection proposing prompt/skill/schedule improvements, sandboxed, git-versioned rollback.
- ⏳ **E5 — Shell.** Voice-first simple GUI: status orb, emotion readout, clock, notifications,
  privacy shutter tile. Simple enough for a beginner, loved by a student.

## Track M — Owner milestones

- ✅ **M0 — The stick exists.** `nova.hdd` builds, verifies, boots (QEMU-verified BIOS+UEFI);
  write with Rufus/`dd` to any 64 MB+ pendrive. Boot-chain trust pins
  (`tools/manifest/bootchain.sha256`, `scripts/trust.sh`) hash every third-party binary that
  enters the image; `scripts/check.sh` runs trust → build → images → host tests → structural
  verify → QEMU boot tests, and `.github/workflows/check.yml` runs it all on every push.
- ⏳ **M1 — Daily driver.** Minimal-Linux image (Buildroot): boots real laptops to the NOVA
  shell with NovaEyes running; camera, mic, audio, keyboard all live. ~300 MB, no daemons.
- ⏳ **M2 — Study pipeline.** maths-pack indexing (PDF + video + handwriting), study_session
  skill, quizzes, charts, case memory.
- ⏳ **M3 — Module manager.** Demand list → signed shuttle → install → capability registry;
  music, emulator/game cores, and Linux-app modules ride the same protocol.
- ⏳ **M4 — Self-improvement.** Metamorph nightly reflection with sandboxed validation and
  rollback; the OS visibly gets better at serving its owner.
- ⏳ **M5 — The full circle.** NovaCore runs on Nucleus; the Linux scaffolding retires.
