# Progress Log
Newest entry first. Each entry: Done / In progress / Next / Blockers.

## 2026-09-26 - C4 redesigned on Limine's built-in capabilities
- Done:
  - The old C4 design assumed Limine could verify Ed25519 signatures (it can't). Replaced with a design using Limine's actual `path#blake2b` file hashes.
  - Manifest gains a `bootchain` section with Limine and kernel file hashes.
  - `docs/bootchain-signing-design.md` written: architecture, integration with `build-disk.sh`, verification flow, testing strategy.
  - `docs/manifest-format.md` updated with the `bootchain` section rules.
  - Limine 12.9.0 already supports `path#blake2b` (`.freebuff/ref/limine/limine-12.9.0/CONFIG.md:435`); no changes to Limine needed.
- Not yet done: updating `create-manifest.py` to accept and validate `bootchain` fields, updating `build-disk.sh` to compute and embed hashes.
- Next (next session):
  1. Update `tools/create-manifest.py` to accept `--limine-hash`, `--limine-size`, `--kernel-hash`, `--kernel-size`.
  2. Update `tools/verify-manifest.py` to verify bootchain files when `--files DIR` is passed.
  3. Update `scripts/build-disk.sh` to compute hashes and update the Limine config.
  4. Test: build with mismatched hashes, boot in QEMU, verify Limine halts.
  5. W3 hardware matrix (real machines).
- Blockers: none (C3 atomic install can wait; it depends on C4's completion).

## 2026-09-25 - C1/C2 rebuilt: standard-library signing tools checked against OpenSSL
- Done:
  - The audit is committed (`4f36bf0`).
  - `tools/nova_trust.py` provides Ed25519 (RFC 8032 §5.1), RFC 8410 PEM key files, the strict manifest v1, and replay state.
  - `tools/keygen.py` is new, and `create-`/`sign-`/`verify-manifest.py` were rewritten.
  - `sign-release.py`, `verify-release.py` and `prepare-release.py` were removed (the second signing format and the `cryptography` dependency).
  - `docs/manifest-format.md`, `docs/root-key-ceremony.md` and `kernel/trust/README.md` now match the code.
  - `tests/test_trust.py` was added to `check.sh` stage 4.
- Evidence, from real runs:
  - `python tests/test_trust.py` → `Ran 24 tests ... OK`
  - `python tests/mutate_trust.py` → 11/11 planted bugs caught (`docs/logs/c1c2-trust-mutations.log`)
  - `bash scripts/check.sh` → `PASSED WITH 3 SKIPPED`: the oracles are absent on Windows, and all 4 QEMU boots pass (`docs/logs/c1c2-check-full.log`)
- Not verified: CI hasn't run this yet, because nothing is pushed.
- Next, in order:
  1. Push (needs the owner's OK) and confirm the Linux CI run executes `test_trust.py`.
  2. **Owner:** run the ceremony in `docs/root-key-ceremony.md` on an offline machine, then commit `kernel/trust/root-key.pub`. C1 isn't done until then.
  3. C4 redesign on Limine `path#blake2b` + `enroll-config`, then C3 (A/B on top of it).
  4. `mathd` rebuild, blocked on the owner's linker decision.
  5. Junior: fix the stale `docs/roadmap.md:7` citation; finish the K2 exception-vector gate (`kernel/src/exctest.rs`, `gdt.rs`, uncommitted).
- Blockers: the linker decision for `mathd`; push permission.

## 2026-09-25 - Junior's W2-R, CI-1 and W3 draft verified
- Done (junior, 2026-09-24, pushed):
  - **W2-R:** `fuzz-disk.py` now aborts unless the unmutated image passes. CI fuzz result: 302/10,000 detected, 0 misses in exact-checked ranges, 245 presence-only passes, 9,453 no-invariant passes (the honest distribution).
  - **CI-1:** CI is green for the first time (branch run 36001422533, `main` run 36002383838).
    - Changes: `limine` built from the vendored `limine.c`, QEMU stderr captured, OVMF booted via pflash.
    - All 4 QEMU boots pass. The external checkers (`sgdisk`, `fsck.fat`, `mdir`) now run in CI, and they caught 2 real FAT bugs that the in-house verifier had missed: dot entries (ECMA-107 §7.3) and a missing volume-label entry. Both are fixed in `make-esp.py`.
    - `check.sh` now reports skips instead of claiming "ALL PASSED".
  - **W3:** `docs/hardware-matrix.md` drafted. The QEMU rows are PASS with evidence; real machines are UNTESTED.
- How it was verified: `gh run view 36002383838 --log`, stages 6–8, and `grep -n baseline tools/fuzz-disk.py`.
- Still stale: `docs/roadmap.md:7` lists the void `docs/logs/w2-fuzz-10k.log` as "0 false passes", while line 29 of the same file says it's void. Junior should fix it.
- In progress (junior, uncommitted): `kernel/src/exctest.rs` and `kernel/src/gdt.rs`, apparently the K2 gate (fire all 32 exception vectors).
- Next: the assistant commits the audit, then rebuilds C1/C2 (in progress now). The owner still has to decide how `mathd` gets tested and run W3 on real machines.

## 2026-09-24 - Audit: every "done" claim re-checked by running it
This entry corrects the two entries below. Where they disagree, this one and the logs win.
- Verified OK: `novacore` 23/23 (`cd novacore && PYTHONPATH=src python -m unittest discover -s tests`). Kernel builds, and SeaBIOS + OVMF reach `nova>` on this Windows host. `tools/verify-disk.py` passes on the real image.
- Broken (raw transcripts in `docs/logs/audit-2026-09-24-mathd.log` and `docs/logs/audit-2026-09-24-fuzz-and-c3.log`):
  1. **W2 fuzz is void.** `fuzz-disk.py` passes the image as a `bytearray`. The W2 change keys a dict by GUID slices, which raises `TypeError: unhashable type: 'bytearray'` on the *unmutated* image, so all 10,000 iterations and 54/54 sanity probes count as "detected". The coverage map says 94.5% of the image has no invariant, so an honest run would show about 9,400 legitimate passes.
  2. **CI has never passed.** Run #35967569558 failed because the only vendored `limine` tool is a Windows `.exe` (UEFI-only image on Linux), fuzz and oracle crashed on import, and all 4 QEMU boots timed out with empty serial. `test-boot.sh` discards QEMU stderr, so those can't be diagnosed. W2 commit `46771d6` is not pushed.
  3. **mathd has never compiled** (3 lib errors, 7 lib-test errors; `sha2`/`chrono` deps break std-only; no SymPy fixtures; B8 invariant not enforced). This host has no MSVC/Windows SDK, so nothing links. `cargo check` still works.
  4. **C1–C4:** the tests never import the tools, replay protection isn't implemented, `cryptography` was added without approval, the C3 tool crashes (`unpack requires a buffer of 52 bytes`), and 9 cited files don't exist. C4's design assumes Limine verifies an Ed25519 manifest, which it can't. Limine 12.9.0 *does* support `path#blake2b` and `limine enroll-config` (`.freebuff/ref/limine/limine-12.9.0/CONFIG.md:435`).
  5. `check.sh` prints `ALL PASSED` while 3 oracles SKIP. The final line should count skips.
- In progress: nothing from the audit is committed yet. Uncommitted: the two audit logs and these three files.
- Next, in order:
  1. Owner decides how `mathd` gets tested: install a host linker (VS Build Tools, or `rustup toolchain install stable-x86_64-pc-windows-gnu`), or test `mathd` in CI only.
  2. Junior, **W2-R**: fix the dict key (`bytes(...)`), make `fuzz-disk.py` abort unless the unmutated image passes, re-run, recommit the log, and correct the README/roadmap numbers. Then **CI green**: build `limine` from the vendored `limine.c` on Linux and pin it in `tools/manifest/bootchain.sha256`, log QEMU stderr, and fix OVMF (pflash) on ubuntu-latest. Push, and cite the green run URL.
  3. Assistant: write `docs/audit-2026-09-24.md`, retract `docs/work-order-c*-completion.md` and the `mathd/README.md` claims, and delete the 4 void C-phase tests. Rebuild C1/C2 stdlib-only, checked against OpenSSL-generated fixtures, with real replay protection.
  4. W3 hardware matrix.
- Blockers: the host-linker decision; CI debugging needs pushes.

## 2026-09-24 - Project memory files added
- Done: plan.md, progress.md and decisions.md written from the git history, README, `docs/work-orders.md` and `docs/plan-v2.md`. These three files summarise; `docs/work-orders.md` stays the detailed task list.
- Latest commit: W2 (spec-derived disk verifier, 10k-mutation fuzz, external oracles).
- Uncommitted: `docs/logs/audit-2026-09-24-fuzz-and-c3.log`, `docs/logs/audit-2026-09-24-mathd.log`. Review them and commit if they belong in the repo.
- Next: W3 hardware matrix (boot on QEMU SeaBIOS/OVMF plus two real machines from USB and commit the evidence), then `novacore` (the next item in the README), then the deferred review items.
- Blockers: W3 needs real hardware and a USB stick.
- Rule reminder: under AGENTS.md, nothing is marked done without the exact command and its real output.

## 2026-09-22 - Trust model designs, mathd B1–B8, W4–W6
- Done: C1–C6 design docs; mathd parser → card store; capability boundary, owner content kept off disk, chained logs; K2 interactive console; MIT licence; pinned Limine tarballs.
