# NOVA — Architecture (v0.1)

Two tracks, one destination. This document describes what exists in code today and the seams
where everything that comes later attaches.

## Track A — Nucleus kernel (kernel/)

```
┌────────────────────────────────────────────────────────────┐
│ Limine v12.9.0 (UEFI + BIOS)                               │
│  loads nova.iso → kernel ELF (limine protocol, base rev 3) │
└──────────────┬─────────────────────────────────────────────┘
               │ framebuffer, memory map, HHDM, stack
┌──────────────▼─────────────────────────────────────────────┐
│ nucleus (Rust no_std, x86-64)                              │
│  limine.rs      request anchors + response structs         │
│  serial.rs      COM1 115200 8N1 logging                    │
│  framebuffer.rs direct pixel drawing, RGB/BGR masks        │
│  font.rs        8x8 bitmap font (FontAtlas::embedded)      │
│  draw.rs        NOVA wordmark: centered text + orb         │
│  panic.rs       fault reporter (serial + framebuffer)      │
│  qemu_exit.rs   isa-debug-exit (QEMU-only clean shutdown)  │
└────────────────────────────────────────────────────────────┘
```

Boot flow: `_start` (NakedFn, SysV) → align stack → save boot-info pointer → `kmain`.
`kmain` waits for the Limine framebuffer response, writes the boot report to serial
(culminating in `NOVA_BOOT_OK`), draws the logo, then idles (`hlt` loop). Under QEMU with
`-debugcon` wired to `isa-debug-exit`, `qemu_exit::success()` ends the machine cleanly so CI
can assert on the serial log.

Request anchoring: the linker script places `.limine_requests_start` (4×u64 magic) before the
request structs and base-revision tag, and `.limine_requests_end` (2×u64) after, all in one
8-byte-aligned section. `linker_assert!` checks the payload stays in `[start, end)`.

## Track B — NovaEyes (planned `eyes/`) — DESIGN ONLY, NOT BUILT

> **Status (W1 #8):** **No `eyes/` directory exists in the tree.** Everything in
> this section is a design for unwritten code, written to fix the port seams
> early. Do not cite it as existing behaviour. When E0 lands, this notice comes
> down and the text is updated to the present tense with evidence.

```
camera (cv2.VideoCapture, MJPG@640x480)
   │ frames stay in RAM; never serialized to disk
   ▼
detector.load(...)          # YuNet face + HSEmotion ONNX (lazy, cached)
detector.detect(frame)      # → Detection(x, y, w, h, label, score) | None
   │                        # detect() is a pure function — the K5/K6 port seam
   ▼
stabilizer.update(detection)  # hysteresis + debounce → StableState
   ▼
greetings.render(state, now)  # time-aware spoken line, spoken cooldown
   ▼
journal.record(label, score)  # text line: utc, emotion, confidence
speak.say(text)               # Piper TTS subprocess; --no-speak skips silently
```

Design rules for the (unwritten) implementation to keep the port path open:
- `detect.py` is to be deliberately the only module that knows models exist. On Nucleus, that
  module becomes a thin C++ shim over the same ONNX files.
- All hardware access (camera, audio) is behind `camera.py` / `speak.py` so a kernel driver
  replaces a driver call, not a policy decision.
- Tier detection (`config.detect_tier`) is the seed of the Thrift governor.

## Future seams (drawn now, filled later)

- **Shuttle (pendrive channel):** package format = dir with `manifest.json`, `payload.bin`,
  `manifest.sig` (ed25519). K3's FAT32 driver is its in-kernel reader; the Python packer/verifier
  lands with the Eyes v0.2 release.
- **Metamorph (self-evolution):** nightly reflection writes *proposals* only; promotion is
  explicit, sandboxed, and git-versioned.
- **Time:** the eyes journal and greetings already localize to morning/afternoon/evening/night;
  the kernel gets an RTC driver in K3.

## Capability Table & Enforcement Status (K4-R3 F7)

AGENTS.md invariant: "Capabilities are explicit. A process or module gets what its manifest declares and nothing else. Ambient authority is a bug."

In accordance with AGENTS.md rule 9 ("No placeholder presented as real") and rule 10 ("Report counts that match reality"), the table below documents every resource in the capability vocabulary, what kernel implementation exists today, and what each capability actually gates:

| Wire Capability Name | `Resource` Arm | Kernel Subsystem Status | What It Actually Gates Today | Future Gate Location |
|:---------------------|:---------------|:------------------------|:-----------------------------|:---------------------|
| `device:camera` | `Resource::Camera` | None (no camera driver or MMIO mapping) | **Gating nothing** (token check only) | Camera device open/capture syscall |
| `device:microphone` | `Resource::Microphone` | None (no audio input driver or DMA buffer) | **Gating nothing** (token check only) | Audio record syscall |
| `device:audio` | `Resource::Audio` | None (Intel HDA tone driver planned for K5c) | **Gating nothing** (token check only) | Audio playback/tone syscall |
| `fs:read` | `Resource::FsRead` | In-kernel FAT16 (`fat.rs`) and FAT32 (`fat32.rs`) | **Token check only** (in-kernel reader exists; ring 3 user file syscalls deferred to K6-0) | Ring 3 `sys_open` / `sys_read` (K6-0) |
| `fs:write` | `Resource::FsWrite` | None (kernel is strictly read-only) | **Gating nothing** (denied for all callers) | Read-only kernel invariant |
| `compute:expression` | `Resource::ComputeExpression` | None (mathd evaluator runs in user space) | **Gating nothing** (token check only) | Math evaluation RPC / service gate |
| `compute:verify` | `Resource::ComputeVerify` | In-kernel Ed25519 / SHA-256 in `trust.rs` | **Token check only** (ring 0 package auth uses primitives; no ring 3 user compute syscall) | Verification service syscall |
| `reserved` | `Resource::Kernel` | Kernel supervisor authority | **Gated permanently** (refused by `from_manifest`, ungrantable) | Kernel internal operations |
