# Plan — NOVA

> Summary only. The detailed task list is `docs/work-orders.md`, the vision is `docs/plan-v2.md`, and the rules are `AGENTS.md`. If this file conflicts with them, they win.

## Goal
A local-first AI OS that serves one owner only. Everything runs on the machine and nothing phones home. New models and knowledge arrive on a signed pendrive and are verified before install.

## Scope
- In: an x86-64 kernel written from scratch (Nucleus), a BIOS+UEFI pendrive image, `mathd` (a verified maths engine), `novacore` (the AI layer), and a trust chain (signing, atomic install).
- Out for now: Apple Silicon (VM only), Wi-Fi (off by default), any network features.

## Architecture / approach
- `kernel/`: a Rust `no_std` kernel booted by Limine v12.9.0.
- `build/nova.hdd`: built by pure-Python tools and checked by `tools/verify-disk.py`, which is written from the GPT/FAT specs. External oracles (sgdisk, fsck.fat, mdir) run in CI (`scripts/check.sh` stage 7) and SKIP loudly on hosts without the tools, such as this Windows machine.
- `mathd/`: intended as a std-only Rust maths engine, each part checked by an independent oracle (SymPy fixtures, finite differences, mutation testing). **As of 2026-09-24 none of that is true yet**: it depends on `sha2` + `chrono`, has no SymPy fixtures, and does not compile (see progress.md). *Superseded 2026-10-09: rebuilt std-only; B1 and B2 pass in CI (see Milestones).*
- `novacore/`: Python AI layer (camera → emotion → voice → journal; skills; case memory).
- Short term, a minimal Linux base does the daily-driver work; Nucleus replaces it later.

## Milestones
- [x] W0 repo + CI (clippy -D warnings, fmt). First green run 2026-09-24: `main` #36002383838
- [x] W1 truth pass
- [x] W2 spec-derived disk verifier + fuzz with baseline guard + external oracles run in CI (W2-R). Evidence: `docs/logs/w2r-*.log`, `docs/logs/ci1-run-36001422533-green-stage7-8.log`. The original `w2-fuzz-10k.log` is void.
- [ ] W3 hardware matrix: drafted in `docs/hardware-matrix.md`. QEMU rows PASS; real machines UNTESTED (need the owner's hardware and a USB stick)
- [x] W4 capability boundary; W5 owner content off the disk; W6 tamper-evident logs
- [ ] B1–B8 mathd: code committed but **does not compile** (`cargo check`: 3 errors in lib, 7 in lib test); no test has ever run; B3/B6 fixtures absent; B8 "unverified card is unconstructable" not enforced. Evidence: `docs/logs/audit-2026-09-24-mathd.log`. **Superseded 2026-10-09:** crate rebuilt; B1 and B2 pass in CI (mathd run 37609824617 at fb10e85: 37 + 3 + 1 = 41 passed, 0 failed). B3 in progress in another session. B4–B8 not started.
  - [x] B1 parser and AST (CI, see progress.md 2026-10-06)
  - [x] B2 canonicaliser and structural hash (CI run 37609824617)
- [ ] C1 root key: ceremony doc + `tools/keygen.py` done (stdlib-only). **The owner has not generated the key yet**, so `kernel/trust/root-key.pub` doesn't exist.
- [x] C2 signed manifests: payload hashes, replay/rollback protection, explicit capabilities (`tools/nova_trust.py` + 3 CLIs). Evidence: `docs/logs/c1c2-check-full.log` (24 tests), `docs/logs/c1c2-trust-mutations.log` (11/11 planted bugs caught). Not yet run in CI.
- [x] C3 atomic install: the committed tool crashes and the design can't switch slots. Needs a redesign (depends on C4). **Update 2026-10-09:** redesigned 2026-10-05 (A/B slots, directory-entry pointer). Stage 14 passes locally, except one run where both OVMF boots timed out (work order R6); in CI it crashed on an unset `LOCALAPPDATA` (fixed 2026-10-09). Tick when a CI run shows `c3-atomic-install oracle: PASS` (work order R1). CI run 37935532518 shows PASS, but both OVMF boots were skipped (firmware path, fixed). Tick after a run with both OVMF boots. **Done 2026-10-09:** CI run 37936764209 (commit 821b283) shows both OVMF boots PASS, no SKIP, `c3-atomic-install oracle: PASS` (`docs/logs/ci-37936764209-stage14.log`). R6 (local OVMF timing margin) stays open.
- [x] C4 boot-chain signing: redesigned on Limine's `path#blake2b` file hashes. Manifest gains `bootchain` section with Limine and kernel hashes. `docs/bootchain-signing-design.md` written.
- [ ] C5 reproducible builds: hashes in manifest must be reproducible (depends on C4).
- [ ] C6 Secure Boot: optional; depends on C5 and D5 threat model.
- [x] K1 boots; K2 interactive console (SeaBIOS + OVMF reach `nova>` locally and in CI, `main` run #36002383838 stage 8)
- [ ] K2 gate: every one of the 32 exception vectors fired by a test (✓ done 2026-09-26, K2 exception gate met)
- [x] K3 memory management (closed 2026-10-09 by R2, CI run 37944150122):
  - [x] K3a frame allocator, 4-level page tables, kernel heap (2026-09-26): frame list, contiguous 2 MiB runs, heap allocator, 28-check memory gate. Evidence: `docs/logs/k3a-*.log`
  - [x] K3b guard pages, FAT32 vs mdir, CMOS RTC (K3 gate complete 2026-09-27: guard-page fault proof `docs/logs/k3b-guard-mutation-proof.log`; FAT32 vs mdir oracles + mutation proof `docs/logs/k3c-*.log`; RTC gate + host-UTC bracket oracle and three mutation proofs `docs/logs/k3d-*.log`). **Reopened 2026-10-09:** the FAT32-vs-mdir part never passed. mdir was SKIPPED in the cited log (`docs/logs/k3c-check-full.log:370`) and prints DIFFER in every CI run, because the corpus is not valid FAT32 (1014 clusters, 512-entry FAT). Work order R2. **Closed again 2026-10-09** by R2 (see the FAT32 line below).
    - [x] guard pages (`docs/logs/k3b-guard-mutation-proof.log`)
    - [x] CMOS RTC (CI stage 10 IN BRACKET, run 37610319211)
    - [x] FAT32 read byte-identical to mdir (R2). 2026-10-09: corpus is now real FAT32 (66000 clusters, beea55f); the kernel refuses non-FAT32 and a read_dir early-stop bug is fixed (be6c7ea, `docs/logs/r2-step3-kernel.log`); fat gate 22/22 locally. Done 2026-10-09: CI run 37944150122 (commit 6edec4b) stage 9 prints `fsck.fat exit 0`, `mdir-vs-kernel: IDENTICAL` over 20 listing lines in 3 directories, and `SABOTAGE GATE PASS` (`docs/logs/ci-37944150122-stage9.log`).
- [ ] K4–K7 kernel ladder (capabilities → USB/display/audio → mathd on Nucleus → camera/voice). K4a capability table, K4a shell oracle and K4b ELF64 loader pass locally (`docs/logs/k4a-*.log`, `docs/logs/k4b-*.log`). Their CI stages 11–13 always skipped until the 2026-10-09 QEMU-lookup fix; first Linux PASS in CI run 37935532518 (`docs/logs/ci-37935532518-stages11-14.log`).
- [ ] Deferred review items (prompt budget, event_id collision, iter_summaries, memory search normalisation, rollback_plan comment)
- [ ] Phase D release gates: D1–D6
