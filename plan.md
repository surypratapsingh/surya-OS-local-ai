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
- `mathd/`: intended as a std-only Rust maths engine, each part checked by an independent oracle (SymPy fixtures, finite differences, mutation testing). **As of 2026-09-24 none of that is true yet**: it depends on `sha2` + `chrono`, has no SymPy fixtures, and does not compile (see progress.md).
- `novacore/`: Python AI layer (camera → emotion → voice → journal; skills; case memory).
- Short term, a minimal Linux base does the daily-driver work; Nucleus replaces it later.

## Milestones
- [x] W0 repo + CI (clippy -D warnings, fmt). First green run 2026-09-24: `main` #36002383838
- [x] W1 truth pass
- [x] W2 spec-derived disk verifier + fuzz with baseline guard + external oracles run in CI (W2-R). Evidence: `docs/logs/w2r-*.log`, `docs/logs/ci1-run-36001422533-green-stage7-8.log`. The original `w2-fuzz-10k.log` is void.
- [ ] W3 hardware matrix: drafted in `docs/hardware-matrix.md`. QEMU rows PASS; real machines UNTESTED (need the owner's hardware and a USB stick)
- [x] W4 capability boundary; W5 owner content off the disk; W6 tamper-evident logs
- [ ] B1–B8 mathd: code committed but **does not compile** (`cargo check`: 3 errors in lib, 7 in lib test); no test has ever run; B3/B6 fixtures absent; B8 "unverified card is unconstructable" not enforced. Evidence: `docs/logs/audit-2026-09-24-mathd.log`
- [ ] C1–C6 trust model: C1–C4 tools committed but their tests never import the tools; replay protection claimed but not implemented; C3 swap tool crashes on a spec-shaped pointer block; 9 cited files do not exist. C5/C6 are designs only.
- [x] K1 boots; K2 interactive console (SeaBIOS + OVMF reach `nova>` locally and in CI, `main` run #36002383838 stage 8)
- [ ] K2 gate: every one of the 32 exception vectors fired by a test
- [ ] K3–K7 kernel ladder (paging/FAT32 → processes/capabilities → USB/display/audio → mathd on Nucleus → camera/voice)
- [ ] Deferred review items (prompt budget, event_id collision, iter_summaries, memory search normalisation, rollback_plan comment)
- [ ] Phase D release gates: D1–D6
