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
  A memory selftest gate runs in five phases around the CR3 switch — allocator
  vs the memory map, software walks pre-switch, heap invariants re-derived from raw window
  bytes, CPU-visible round-trips post-switch, then stack guard pages — and exits 33 together
  with the exception gate in one boot (`docs/logs/k3a-exception-and-memory-gate.log`),
  27-pass/1-fail with exit 35 when an expectation is mutated
  (`docs/logs/k3a-memory-gate-mutation-proof.log`). The cross-check oracle caught a real
  defect: the walk's 2 MiB base mask was one hex digit short, which only showed because the
  CPU's writes and the software walk disagreed.
- 🟡 **K3 — Memory + files (slice 2 landed: boot-stack guard pages).** The page below the
  boot stack is mapped not-present on every boot (copy-then-edit machinery in `paging.rs`:
  the owning PDPT/PD/PT is privatized verbatim, 1 GiB / 2 MiB leaves are split, the edit is
  software-verified against pre-edit translations, and the TLB is flushed with a PGE-aware
  full reload — Limine's tables are never written). #PF is routed to IST3, so the first
  IST-consuming delivery in this kernel's history is exercised by fire: arming it exposed
  two latent K2 bugs — the TSS descriptor dropped `base[31:24]` and the TSS struct's layout
  put every IST field 4 bytes off the CPU's fixed offsets — both fixed with a compile-time
  size assertion and a descriptor readback. Under `novatest`, phase 5 overflows the real
  stack deliberately: the #PF arrives with `cr2 == guard + 0xFF8` and error code `0x2`
  (not-present write), the last legal store lands at the stack's last legal address, and the
  physical page below the guard is byte-identical to a pre-probe copy. Evidence:
  `docs/logs/k3b-selftest.log` (exit 33; mem gate 36 passed / 0 failed),
  `docs/logs/k3b-guard-mutation-proof.log` (mutated cr2 expectation → exit 35, 35/36).
  Still open for the K3 gate: read-only FAT32 verified byte-identically to `mdir` over a
  generated corpus, CMOS RTC.
- 🟡 **K3 — Memory + files (slice 3 landed: read-only FAT32 + mdir-format corpus).** A
  deterministic stdlib-only generator (`tools/gen-fat32-corpus.py`, spec-cited, sha256-stamped)
  builds a 2 MiB FAT32 image (1014 clusters, fixed 2026-09-01 12:34:56 stamp) covering 15
  directory-layout cases — volume label, plain 8.3, lowercase and mixed-case LFN, 2- and 3-slot
  LFN, RO|HIDDEN, unicode names, nested directories, an empty dir, and a 64-LFN-entry directory —
  with per-case expectations in `tools/fat32-corpus-manifest.json`. The image rides into the
  kernel as a Limine module (`module_path: boot(1):/fat32-corpus.img`, 4 KiB-aligned per
  PROTOCOL.md "Module Feature"), where a read-only driver (`kernel/src/fat.rs`: BPB validation,
  cluster-chain walk with a step budget, LFN reconstruction with checksum validation and UTF-8
  surrogate rejection, and an exact `mdir`-format port from the vendored mtools `dir.c`/`config.c`
  defaults — 24-hour clock, `am_pm = ' '`) passes a 22-check fat gate
  (`kernel/src/fatselftest.rs`). Layered oracles: `tools/verify-fat32.py` re-derives the
  filesystem from the FAT32 spec independently of both the generator and the driver and renders
  the same FATLIST blocks — the kernel's serial output and the checker agree byte-for-byte
  (3 dirs, 16 file lines, including the trailing am_pm space). Mutation proof: cutting the chain
  walk after the first cluster fails the gate 20/22 with exit 35
  (`docs/logs/k3c-chain-mutation-proof.log`). The 9-stage `scripts/check.sh` regenerates the
  corpus and `cmp`s it byte-identical, then runs `fsck.fat -n` and the real `mdir` against the
  kernel's listing — both oracles run only in CI; without mtools locally they SKIP loudly.
  Evidence: `docs/logs/k3c-fat-selftest.log` (exit 33; fat gate 22 passed / 0 failed),
  `docs/logs/k3c-verify-fat32.log`, `docs/logs/k3c-check-full.log`.
  Still open for the K3 gate: CMOS RTC.
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
  enters the image; Limine itself verifies the kernel: `scripts/build-disk.sh` substitutes a
  real 128-hex-char blake2b-512 digest of the kernel ELF into the config's `path:` line (no
  algorithm prefix — vendored `uri.c` panics otherwise), and `verify-disk.py` section 7
  re-derives the digest with hashlib and cross-checks it against the embedded config. Stage 5
  grew from 50 to 52 checks; the config oracle compares against the builder's substituted
  sidecar (`build/limine.conf.<image>`), never the raw `kernel/limine.conf`.
  `scripts/check.sh` runs trust → build → images → host tests → structural verify → QEMU
  boot tests, and `.github/workflows/check.yml` runs it all on every push.
- ⏳ **M1 — Daily driver.** Minimal-Linux image (Buildroot): boots real laptops to the NOVA
  shell with NovaEyes running; camera, mic, audio, keyboard all live. ~300 MB, no daemons.
- ⏳ **M2 — Study pipeline.** maths-pack indexing (PDF + video + handwriting), study_session
  skill, quizzes, charts, case memory.
- ⏳ **M3 — Module manager.** Demand list → signed shuttle → install → capability registry;
  music, emulator/game cores, and Linux-app modules ride the same protocol.
- ⏳ **M4 — Self-improvement.** Metamorph nightly reflection with sandboxed validation and
  rollback; the OS visibly gets better at serving its owner.
- ⏳ **M5 — The full circle.** NovaCore runs on Nucleus; the Linux scaffolding retires.
