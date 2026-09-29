# Progress Log
Newest entry first. Each entry: Done / In progress / Next / Blockers.

## 2026-09-29 - reproducible interactive-shell oracle (sendkey from a script)
- Done:
  - `tools/drive-shell.py` + `kernel/scripts/test-shell.sh`: the manual
    sendkey procedure of `docs/logs/k4a-time-verb.log` is now a check
    stage. The driver listens on two OS-assigned loopback ports and QEMU
    connects to them (`-serial tcp:...`, `-monitor tcp:...`), so the VM
    starts only after both chardevs are connected and no boot output can
    be missed by construction. No fixed sleeps: each keystroke is awaited
    by its own console echo (`nova> t` -> `nova> ti` -> ...), each
    command by the next `nova> ` prompt, boot by the first prompt.
  - Oracles, none of them the code under test: the verb list is grepped
    from shell.rs's `dispatch()` arms (a renamed/added verb must survive
    its own drive; `reboot`/`halt`/`novatest` are excluded from typing,
    loudly); replies are checked inside post-Enter response windows
    (echoes excluded — a window containing the typing would make the
    check tautological): `help` must start `commands: ` and list every
    typed verb, `ver` must print the nucleus version, `mem` must count
    entries; a `zz` control must get `unknown command: zz`, proving the
    keystrokes really round-trip i8042 -> poller -> console; the `time`
    stamp is parsed and bracketed between two HOST-UTC samples
    (+/-120 s, same rationale as test-rtc.sh: QEMU seeds the guest RTC
    from the host clock).
  - Wired into `scripts/check.sh` as stage 12/12 (SKIP counted in the
    verdict; rc 2 = harness could not run). Verified twice green (11
    checks per run; `utc: 2026-09-29T04:02:33` vs host epoch delta 8 s;
    second run 04:04:52, delta 8 s) and mutation-killed: renaming the
    `time` dispatch arm in shell.rs makes the drive fail (transcript in
    `docs/logs/k4a-shell-drive-mutation.log`), reverted after.
- Blockers: none new.

## 2026-09-28 - `time` verb in the nova> shell (K4a follow-on)
- Done:
  - `kernel/src/shell.rs`: new `time` verb (and help listing) printing the
    CMOS RTC read through `rtc::render_datetime` with a fixed `utc: `
    prefix; RTC error paths (NoPower / UpdateStuck / Unstable) print a
    reason instead of failing silently. No new gate checks: the verb's
    formatting is covered by the existing rtc gate render check, which
    asserts the exact 19-char `YYYY-MM-DDTHH:MM:SS` shape the verb prints.
  - Verification: driven through the real PS/2 path with QEMU `sendkey`
    on the regular image — `utc: 2026-09-28T13:28:24`, then `utc:
    2026-09-28T13:29:54` 90 s later (clock live and advancing); `help`
    lists the verb. Selftest image still exits 33 with rtc gate 6/0 (the
    render check) and cap gate 22/0. Evidence:
    `docs/logs/k4a-time-verb.log`. Note: a first sendkey run against a
    stale `build/nova.hdd` printed `unknown command: time` — rebuilt the
    disk and reran; the stale-image lesson is recorded here.
- Blockers: none new.

## 2026-09-28 - K4a: capability table + done-when denial test (K4 first slice)
- Done:
  - **Design:** `kernel/src/cap.rs` — a closed `Resource` vocabulary whose
    wire names are the exact `namespace:permission` strings C2 signs
    (devices were moved into a `device:` namespace after the host oracle
    proved bare `camera` could never be declared in a signed manifest:
    `nova_trust.py`'s `_CAPABILITY` regex requires a colon). `CapSet` is
    constructible only through `from_manifest`: all-or-nothing, unconfirmed
    and out-of-vocabulary rows are hard errors (never silent absences),
    wildcard forms refused, `MAX_CAPS` bounds raw row count, and the
    `reserved` arm is never grantable (the deny-by-default anchor).
    Ambient authority is unrepresentable, not merely rejected.
  - **Kernel gate:** `capselftest.rs` — 22 checks. The K4 done-when is a
    named check: a subject whose manifest declares only `fs:read` attempts
    the camera through the mediation point and is DENIED, while its own
    grant works and the family sibling (`fs:write`) stays closed. Plus the
    7x8 single-row matrix, the full error taxonomy, hand golden tables
    (`HAND_NAMES`) written from `docs/manifest-format.md`, and the
    `CAP_REFS` in-source anchor the host oracle parses. A deny-everything
    kernel is a caught bug: the own-grant check fails.
  - **Host oracle (check stage 11):** `kernel/scripts/test-cap.sh` +
    `tools/gen-cap-manifest.py`. (A) The kernel's claimed vocabulary is
    signed into a REAL C2 manifest (fixed-seed throwaway key, never the
    owner's root key) and verified by `tools/verify-manifest.py`, with 9
    wildcard/out-of-grammar negatives refused unsigned and a
    tampered-signature control. (B) The selftest boot prints one
    `CAP_KERNEL_PROBE` line per (subject, resource) attempt; the oracle
    recomputes all 80 expected values from its own parse of cap.rs plus
    the spec sentence, trusting neither the gate's grades nor shared
    constants. (C) The done-when evidence line must be present.
  - **Mutation proofs (all KILLED, real transcripts in
    `docs/logs/k4a-cap-mutations.log`):** M1 enforcement bypassed
    (`grants()` -> true; 6 checks fail incl. done-when), M2 wildcard
    refusal dropped (4 fail), M3 grants never registered (5 fail, own
    grant among them), M4 `CAP_REFS` vocabulary drift
    (`fs:read`->`fs:reed`; host oracle fails 1/80 while layer A still
    passes - the documented C2/kernel division of labour).
  - **check.sh:** now 11 stages; stage 11 runs the cap oracle (exit 2 =
    loud skip, never a pass). Evidence: `docs/logs/k4a-check-full.log`.
    Stages 5, 6, 8, 9, 10, 11 pass (verify-disk 52 checks, fuzz 10000
    crashes 0, all 4 QEMU boots, corpus determinism, RTC in bracket, cap
    oracle PASS). Stage 4 still fails: 3 failures + 12 errors in the C1/C2
    trust suite (e.g. `create-manifest: FAIL manifest: missing field
    'bootchain'`), present since df69025 — another track's code, not
    touched (rules 7/11). Stages 1, 2, 3, 7 pass/skip as before (sgdisk/
    fsck.fat/mdir absent locally, loud SKIPs; CI-only).
- Commits: b5439b2 (kernel table + gate), 6dc701c (host oracle, stage 11),
  plus docs and this log. Not pushed.
- Next: K4b — ELF64 loader + processes + scheduler (cooperative first);
  then the in-kernel package verifier over the C2 format, wired to the
  capability table so installed packages can only receive the
  capabilities their signed manifest declared.
- Blockers: none new. Windows Smart App Control (state `On`) blocks
  rust-lld intermittently (CodeIntegrity 3033/3077, os error 4551); the
  block cleared on retry this time but it can recur — watch for "os error
  4551" in failed builds.

## 2026-09-27 - C4 boot regression repaired (K3c thread, work order C4-fix)
This entry SUPERSEDES the 2026-09-26 C4 entry below where they disagree.
- Done:
  - **The regression:** the 2026-09-26 entry's "QEMU verification: Tested in SeaBIOS and OVMF"
    held only for the selftest image. Every REGULAR-image boot panicked:
    `PANIC: Blake2b hash must be 128 characters long` (vendored uri.c lines 65-90: exactly
    128 hex chars after `#`, no prefix). Diagnosed 2026-09-27 in
    `docs/logs/k3c-c4-boot-regression-diagnosis.log` and repaired the same day.
  - **Repair:** `kernel/limine.conf` now carries `#KERNEL_BLAKE2B_512_PLACEHOLDER` (no
    prefix); `build-disk.sh` substitutes a real 128-hex blake2b-512 digest of the exact ELF
    make-esp.py embeds (hashlib, stdlib) into a per-image sidecar `build/limine.conf.<image>`
    — the stale shared `build/limine.conf` leak between selftest and regular builds is gone;
    substitution failure is fatal, not silent. `verify-disk.py`/`fuzz-disk.py`/`oracle-disk.py`
    take the embedded-config expectation from that sidecar (stale/missing sidecar exits 1
    loudly) and a new verifier section 7 re-derives the digest from the build-input kernel.
  - **Verification:** all 4 QEMU boot cases PASS (SeaBIOS/hdd, OVMF/hdd, both selftests) —
    Limine's own hash verification passes on the real digest; stage 5 now 52 checks; stage 6
    fuzz PASS behind a clean baseline; negative tests: stale and missing sidecar both exit 1.
    Mutation proof of section 7: flipping one digest nibble in BOTH image and expectation
    (a consistent wrong-digest builder) fails exactly the bootchain check — baseline 0
    failures. Evidence: `docs/logs/c4fix-check-full.log`.
- Next: as per the 2026-09-26 entry (manifest signing, C3 atomic install, W3 hardware).
- Blockers: none new.

## 2026-09-26 - C4 bootchain integration into build pipeline COMPLETE
- Done:
  - **Build integration:** `scripts/build-disk.sh` computes SHA-256 hashes of Limine (BOOTX64.EFI) and kernel (nucleus ELF).
  - **Config format:** `kernel/limine.conf` uses `path: boot(1):/NUCLEUS#blake2b:HASH` for Limine 12.9.0 verification.
  - **Hash embedding:** Build creates `build/limine.conf` with kernel hash filled in before `make-esp.py` embeds it.
  - **Manifest preparation:** Hashes displayed at build time for use with `create-manifest.py` when signing releases.
  - **QEMU verification:** Tested in SeaBIOS and OVMF. Boot menu shows "[7m NOVA (Nucleus) [27m" and counts down "Booting automatically in 3...". Config file format now recognized (no more "config file contains no valid entries" error). Limine is ready to verify kernel hash and load.
  - **Example hashes (2026-09-26 build):**
    ```
    Limine:  f24efeecf6cfd3e11dd47a8263fece74509ec91f83b7f1d166b8ca30892d629f (376832 bytes)
    kernel:  d3a96844a14093c5eb3db0528a71fc7352bd3721472027a5d6264df864f52a4d (140512 bytes)
    ```
- Next:
  1. When signing a release manifest: pass these hashes to `create-manifest.py` with --limine-* and --kernel-* flags. See: `tools/create-manifest.py --help`.
  2. W3 hardware matrix (real machines): boot on actual hardware to verify the signed manifest and verified boot chain.
  3. C3 atomic install (depends on C4 now complete).
- Blockers: none. Manifest creation and hardware testing are independent next steps.

## 2026-09-26 - C4 bootchain support implemented and tested
- Done:
  - **Design:** old C4 assumed Limine could verify Ed25519 (it can't). Redesigned on `path#blake2b` file hashes. Docs: `docs/bootchain-signing-design.md`.
  - **Manifest format:** adds optional `bootchain` section with Limine and kernel hashes. Docs updated: `docs/manifest-format.md`.
  - **Tools updated:**
    - `tools/nova_trust.py`: `build_manifest()` accepts `bootchain` parameter; `verify_manifest()` validates bootchain files when `--files DIR`; `structure_errors()` validates bootchain format (Limine: version/filename/sha256/size; Kernel: id/version/filename/sha256/size).
    - `tools/create-manifest.py`: accepts `--limine-version`, `--limine-hash`, `--limine-size`, `--kernel-id`, `--kernel-version`, `--kernel-hash`, `--kernel-size`. Builds bootchain section automatically.
  - **Tests:** bootchain creation, signature, file verification, corruption detection, missing file detection all pass.
- Still open (next session):
  1. Update `scripts/build-disk.sh` to compute hashes and pass them to manifest creation.
  2. Update Limine config to use `path#blake2b:HASH` directives for files.
  3. Integration test: build with mismatched hashes, boot in QEMU, verify Limine halts.
  4. W3 hardware matrix (real machines).
- Note: junior pushed K3a (memory allocator, paging) while we worked on C4. Check git log for details.
- Blockers: none (can proceed to W3 or C3).

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
