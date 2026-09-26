# NOVA — Roadmap

Status marks: ✅ done (cites committed evidence under `docs/logs/`) · 🔨 in progress · ⏳ planned
Evidence set: `docs/logs/w1-check-pass.txt` (full 6-stage gate),
`docs/logs/w1-qemu-typing.log` (BIOS boot + interactive shell session),
`docs/logs/w1-font-mutation.log` (deliberate glyph corruption caught by the runtime check),
`docs/logs/w2-fuzz-10k.log` (10,000-iteration verifier fuzz: 0 false passes, 0 crashes),
`docs/logs/w2-detection-demo.log` (the pre-W2 layout defect, reintroduced and caught),
`docs/logs/w2-check-full.log` (full 8-stage gate).
Ladders: **K** = Nucleus kernel · **E** = NovaEyes/NovaCore AI core · **M** = owner-facing
milestones (what you can hold and use). K and E are how we get there; M is the product.

## Track A — Nucleus kernel (`kernel/`)

- ✅ **K1 — It boots.** Limine v12.9.0 (UEFI+BIOS) loads a Rust `no_std` ELF at the canonical
  higher-half base; boot report on serial (`COM1`, ends in `NOVA_BOOT_OK`); NOVA logo drawn to
  the framebuffer with the embedded 8×8 font; clean CI exit under QEMU (`isa-debug-exit`, 33).
  Evidence: `docs/logs/w1-check-pass.txt` (both firmware paths, regular + selftest).
- ✅ **K1.5 — The pendrive.** `build/nova.hdd`: GPT + FAT16 ESP + BIOS-boot partition, built by
  a pure-Python tool (`tools/make-esp.py`), verified structurally by `tools/verify-disk.py`
  (49 checks, GPT layer rewritten from UEFI 2.10 §5.2–§5.3 with the section cited per check —
  including `LastUsableLBA < backup-entries LBA`, the exact check that was missing when the
  layout was wrong), byte-exact file round-trip, BIOS stages installed by `limine bios-install`.
  Boots SeaBIOS and UEFI from one image. `make disk` / `make check`.
  Evidence: `docs/logs/w2-check-full.log` stages 3–5; fuzz after the W2-R repair in
  `docs/logs/w2r-fuzz-10k.log` (10,000 single-byte mutations, clean-baseline gate:
  302 detected, 9,698 legitimate passes — 9,453 in no-invariant space, 245 in
  presence-only regions — 0 exact-invariant false passes, 0 crashes, 54/54 map-sanity).
  The pre-W2-R `docs/logs/w2-fuzz-10k.log` is void: its 10,000/10,000 was an artifact
  (the verifier crashed on the unmutated image; `docs/logs/w2r-fuzz-10k-defects.log`),
  and the honest rerun exposed two real coverage-map defects that W2-R fixed.
  The old defect proven detectable in `docs/logs/w2-detection-demo.log`.
  External oracles (`sgdisk --verify`, `fsck.fat -n`, `mdir` listing vs build inputs)
  run as check stage 7 — skipped loudly on machines without them (the final verdict
  line then counts the skips); they run for real in CI, which installs
  gdisk/dosfstools/mtools. First fully green run: Actions run 36001422533,
  all oracle checks ok — `docs/logs/ci1-run-36001422533-green-stage7-8.log`.
  The oracles found two real builder defects on their first CI execution
  (dot entries written as eleven spaces; boot-sector volume label without a
  matching root entry), both fixed in CI-1.
- ✅ **K2 — Console.** PS/2 keyboard polling (ports 0x60/0x64) with correct controller
  translation handling (the double-translation bug found and fixed via a QEMU monitor probe),
  scancode set 1 → ASCII with Shift/CapsLock, 95-glyph font verified byte-for-byte against
  the reference by `tests/test_font_ref.py` (host) plus a structural runtime check that was
  proven to fire by deliberately corrupting a glyph (`docs/logs/w1-font-mutation.log`),
  format-correct 16/24/32-bpp framebuffer writes, text console with scrolling + serial mirror,
  tiny shell (`help`, `clear`, `mem`, `ver`, `reboot`, `halt`). Interactive typing verified in
  QEMU via monitor `sendkey` (`docs/logs/w1-qemu-typing.log`). CI selftest image (`novatest`
  cmdline → clean exit 33) plus regular-image prompt check.
  **Exception gate (the K2 work-order gate): MET with one documented environment skip.**
  All 32 exception vectors have live gates. 9 are fired NATIVELY — the CPU raises them —
  and asserted against pre-written predictions including exact error codes and CR2:
  #DE(0) div-by-zero, #DB(1) single-step, #BP(3) int3, #UD(6) ud2, #NM(7) TS-set x87,
  #NP(11) with error code 0x28 (not-present data descriptor), #GP(13) with code 0x30
  (beyond-limit selector), #PF(14) with code 0 and CR2 = the probe address, #MF(16)
  0/0 with CW.IM unmasked (delivered on `wait`). The remaining 23 vectors (including
  #DF/#MC/#AC and the reserved vectors, which have no VM-raisable condition) are
  exercised through their real gate entry via a dispatcher that synthesizes the SDM
  Vol. 3 §6.14.2 `int n` frame — asserted gate reachability, honestly NOT claimed as
  CPU-generated faults. The #XM(19) trigger (`divps` 0/0, MXCSR.IM unmasked) is correct
  per SDM Vol. 1 §10.5.3, but QEMU TCG does not deliver unmasked SSE exceptions
  (qemu-project/qemu#215): under QEMU it is recorded as a loud SKIP and counted, never
  as a pass; it fires on KVM/real hardware. Result: 50 checks passed, 0 failed, 1 skip,
  exit 33, on both SeaBIOS and OVMF (`docs/logs/k2-exception-gate-qemu-selftest.log`).
  The suite was proven able to fail: flipping the #GP prediction to 0 produced
  `NOVA_SELFTEST_FAILED` and exit 35 (`docs/logs/k2-exception-gate-mutation-proof.log`).
  The gate work also closed real kernel gaps: a GDT/TSS now exists (IST1→#DF, IST2→#MC),
  and CR0.MP/NE/EM plus CR4.OSFXSR/OSXMMEXCPT are set at boot — Limine leaves them
  clear, so SSE instructions raised #UD before this work.
  Note: `qemu_exit` remains compiled into the binary but is reachable only when the
  bootloader passes the `novatest` command line (`main.rs`), so real-hardware boots idle at
  the shell instead of exiting; a cargo-feature gate is planned with the K2 exception-test
  work.
- 🟡 **K3 — Memory + files (slice 1 landed: allocator, paging, heap).** A physical frame
  allocator chains every `LIMINE_MEMMAP_USABLE` frame from the Limine map (129,621 frames
  under QEMU; the 4 MiB boot stack sits in a bootloader-reclaimable region, so the probe
  checks prove it is never handed out). The kernel builds its own 4-level page tables while
  Limine's are live, inheriting every present PML4 entry by reference (framebuffer, direct
  map, and kernel mapping survive the CR3 switch), puts its mappings under the highest free
  PML4 slot, and switches CR3 to them at boot. A boundary-tag kernel heap owns a 32 MiB
  window backed by contiguous 2 MiB runs (`alloc_run_2m`) mapped with PD-level PS=1 leaves.
  A 28-check memory selftest gate runs in four phases around the CR3 switch — allocator
  vs the memory map, software walks pre-switch, heap invariants re-derived from raw window
  bytes, then CPU-visible round-trips post-switch — and exits 33 together with the exception
  gate in one boot (`docs/logs/k3a-exception-and-memory-gate.log`), 27-pass/1-fail with exit
  35 when an expectation is mutated (`docs/logs/k3a-memory-gate-mutation-proof.log`). The
  cross-check oracle caught a real defect: the walk's 2 MiB base mask was one hex digit
  short, which only showed because the CPU's writes and the software walk disagreed.
  Still open for the K3 gate: stack guard pages with a deliberate overflow faulting ON the
  guard page, read-only FAT32 verified byte-identically to `mdir` over a generated corpus,
  CMOS RTC.
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
