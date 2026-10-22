# NOVA — Super-Senior Architecture Blueprint & 20–40 Hour Sprint (`todo.md`)

> **Date:** 2026-10-22
> **Authority:** Super-Senior Architecture & Verification Review (`AGENTS.md`, `docs/work-orders.md`, `docs/team.md`)
> **Agents:** `antigravity` (Google Antigravity — AG) and `freebuff` (Freebuff — FB)
> **Rule:** Every work order requires (1) the author's `AGENTS.md` end-of-task block with verbatim command output, and (2) the partner agent's adversarial cross-review in `docs/reviews/<wo>-<reviewer>.log` ending `VERDICT: PASS` with at least one planted fault proven caught.

---

## 1. Current Verified Build State & Root-Cause Analysis (2026-10-22)

### Verified Passing right now
1. **`kernel` compilation & lint (`x86_64-unknown-none`):**
   - `cargo check --manifest-path kernel/Cargo.toml --target x86_64-unknown-none` → `Finished dev profile` (exit 0)
   - `cargo check --manifest-path kernel/Cargo.toml --target x86_64-unknown-none --features qemu-exit` → `Finished dev profile` (exit 0)
   - `cargo clippy --manifest-path kernel/Cargo.toml --target x86_64-unknown-none --release -- -D warnings` → exit 0
   - `cargo fmt --manifest-path kernel/Cargo.toml --check` → exit 0
2. **`mathd` compilation & lint (std-only Rust):**
   - `cargo check --manifest-path mathd/Cargo.toml --tests` → `Finished dev profile` (exit 0)
   - `cargo clippy --manifest-path mathd/Cargo.toml --tests -- -D warnings` → exit 0
   - `cargo fmt --manifest-path mathd/Cargo.toml --check` → exit 0
   - Latest `mathd` CI run (`38998831517`, commit `396e319`): 71 tests passed, 0 failed.
3. **`novacore` privacy, capabilities, prompts & D2 zero-socket suite:**
   - `PYTHONPATH=novacore/src py -3 -m unittest discover -s novacore/tests -p "test_*.py"` → 56 tests run (55 passed, 1 POSIX-mode test skipped on Windows; `test_no_sockets.py` 4/4, `test_prompts.py` 4/4, `test_capabilities.py` 6/6).
   - **D2 (R14 G2, commit `12f4946`)** is verified and **ticked** in `plan.md`.
4. **Boot images (`nova.hdd` interactive console):**
   - Stage 8 `SeaBIOS/hdd` and `OVMF/hdd` reach `NOVA_BOOT_OK` and `nova>` cleanly (`build/check-stage8-boottest.log`).

### Verified Failing right now (Why `check` CI is red across runs `38998902987`..`39001115660`)
1. **Blocker A — R13 `#SS` Selftest Hang (`36d7448`, `kernel/src/excselftest.rs` & `kernel/src/idt.rs`):**
   - In `build/qemu-serial.log`, `SeaBIOS/selftest` and `OVMF/selftest` print:
     ```
     exc gate:   arming resume mode; firing 12 native triggers one at a time
       ok    #DE div-by-zero
       ok    #DB single-step
       ok    #BP int3
       ok    #UD ud2
       ok    #NM TS-set x87
       ok    #DF double fault (IST1)
       ok    #NP not-present selector
     ```
     and then hang/triple-fault inside `t_ss` before printing `#SS`.
   - **Root cause (proven from `kernel/src/excselftest.rs:330` and `kernel/src/idt.rs:251`):**
     1. In `t_ss` (`excselftest.rs:330`):
        ```rust
        "mov r12, rbp",
        "mov rbp, {addr}",
        "mov rax, [rbp + 0]",
        "mov rbp, r12",
        ```
        `t_ss` saves the true frame pointer `rbp` in `r12` and sets `rbp = 0x0001_0000_0000_0000` (non-canonical), then executes `mov rax, [rbp + 0]` to raise `#SS` (vector 12).
     2. In `common_exc` (`idt.rs:251–278`), the assembly stub switches `rsp` to `exc_handler_stack_top - 640`, does `fxsave`, and **calls a Rust `extern "C"` function `record_stub`** (`call {record}`) **without saving/restoring general-purpose registers (`r12`, `rbp`, `rax`, `rcx`, etc.)**!
     3. When `mov rax, [rbp + 0]` faults in `t_ss`, `rbp` is STILL `0x0001_0000_0000_0000` (non-canonical)! Inside `record_stub` / `record_native` (or if `r12` or caller-saved registers are touched before `iretq`), `common_exc` returns via `iretq` to `mov rbp, r12` with `rax`, `rcx`, `rdx`, `rsi`, `rdi`, `r8`–`r11` clobbered by `common_exc` (`lea rax`, `mov rdx`, `mov rdi`, `mov rsi`), even though `t_ss`'s `asm!` block only declared `out("rax") _, out("r12") _` and did NOT declare `clobber_abi("C")` or clobbers for `rcx, rdx, rsi, rdi, r8, r9, r10, r11`! Worse: `common_exc` itself clobbers `rax, rdx, rdi, rsi, r9, r10, r11` on EVERY fault!
     4. Specifically, in `t_ss`: `addr = in(reg) NON_CANONICAL` is allocated by LLVM to a register (such as `rcx` or `rdx` or `rsi`), and `common_exc` overwrites `rax, rdx, rdi, rsi, r9, r10, r11` and calls `record_stub` (which may clobber any SysV caller-saved register) and never saves/restores GPRs around `common_exc`! Even simpler: saving/restoring all caller-saved GPRs + `rbp` in `common_exc` (or fixing `t_ss` / `common_exc` GPR preservation) immediately makes resume mode safe across all Rust handler calls.
2. **Blocker B — R9 Build-Script Wiring (`61943ed`, `scripts/check.sh` & `scripts/build-disk.sh`):**
   - `kernel/Cargo.toml` now defines `[features] qemu-exit = []` (off by default).
   - Without `--features qemu-exit`, `qemu_exit::success()` and `qemu_exit::failure()` print their serial line and halt (`cli; hlt`) instead of writing port `0x501`.
   - `scripts/check.sh` (line 80) and `scripts/build-disk.sh` (line 45) still build the kernel once without `--features qemu-exit` and use that same ELF for both `nova.hdd` and `nova-selftest.hdd`.
   - Therefore, `nova-selftest.hdd` never writes `0x501` and times out with `rc=124` instead of exiting with `rc=33`.

---

## 2. How NOVA Will Be Built Next (Future Build Blueprint)

### Track 1: Kernel & Trust Chain (`K4` → `K5` → `C5` → `K6`)
1. **Unblock CI & Selftest Isolation (`R9` + `R13` + `R6`):**
   - Fix `t_ss` `insn_len: 4` in `kernel/src/excselftest.rs` (4-byte `48 8b 45 00` encoding for `mov rax, [rbp + 0]`).
   - Build two distinct kernel binaries:
     - Production kernel (`target/x86_64-unknown-none/release/nucleus`, default features) → packed into `build/nova.hdd`, hash-pinned in `bootchain`, verified by `objdump`/`readelf` in `scripts/check.sh` to contain **zero** writes to port `0x501` (`qemu_exit`).
     - Selftest kernel (`--features qemu-exit`) → packed into `build/nova-selftest.hdd`, exits QEMU via `isa-debug-exit` (`33` pass, `35` fail).
   - Upgrade `scripts/test-atomic-install.sh` (Stage 14) from fixed `timeout 12` to 50 ms serial marker polling with a 30 s cap (`R6`).
2. **Reproducible Builds (`R15` / `C5`):**
   - Audit two back-to-back builds in distinct directories (`build-a/`, `build-b/`).
   - Strip build-path leakage in Rust (`--remap-path-prefix`), pin FAT/GPT timestamps to `SOURCE_DATE_EPOCH` in `tools/make-esp.py`, and derive deterministic GPT partition GUIDs from a fixed seed + content hash.
   - Gate CI on `sha256(build-a/nova.hdd) == sha256(build-b/nova.hdd)`.
3. **K4 Process Isolation, In-Kernel C2 Verifier & Scheduler (`kernel/src/`):**
   - Port Ed25519 + SHA-256 + `manifest.sig` verification (`C2`) into `no_std` Rust inside `kernel/src/trust.rs` so the kernel refuses to load any ELF from `/MODELS` or `/PACKAGES` whose digest or signature does not verify against the embedded public key.
   - Complete the K4 process table (`proc.rs`), Ring 3 `iretq`/`sysretq` transition with per-process PML4 and capability bitmap (`cap.rs`), and APIC timer / cooperative scheduler (`sched.rs`) booting `novad` as PID 1.
4. **K6 `mathd` on Nucleus:**
   - Because `mathd` is strictly std-only with zero I/O or OS syscalls in its core library (`parser`, `canonicalizer`, `evaluator`, `differentiator`, `verifier`, `card_store`), compile `mathd` as a static archive or minimal `no_std`+`alloc` shim linked into a Ring 3 Nucleus task (`mathd.elf`), driven over the kernel IPC channel from `novad` console commands.

### Track 2: Verified Maths Engine (`B5` → `B6` → `B7` → `B8`)
1. **Finish `B5` Verifier (`mathd/src/verifier.rs`):**
   - Fix V7 (`points_tested = 1` for constant equivalence claims, not `8`).
   - Add G1 (test pinning exact `points_tested` so `+8` inflation fails) and G2 (negative parameter sampling for `verify_equation_solution` so `abs(p)*x - p = 0` with root `[1]` is caught as `Failed`).
2. **Build `B6` Adversarial Corpus (`mathd/corpus/`):**
   - Generate 500+ Class 11–12 problems across algebra, trigonometry, differentiation, limits, and equations via a committed SymPy script (`mathd/corpus/generate_corpus.py`).
   - Pair every problem with (a) SymPy-verified ground truth and (b) 2–3 plausible LLM failure modes (missing chain-rule factor, sign error on `cos`/`sin`, lost negative root, extraneous root, wrong constant).
   - Gate CI on **0 false `Verified`** across the entire wrong-answer corpus and **≥ 95% `Verified`** on ground-truth answers.
3. **Build `B7` Prove-It Mode & JSONL Mistake Ledger (`mathd/src/bin/mathd-cli.rs`, `mathd/src/ledger.rs`):**
   - Implement an interactive + batch CLI where a student enters a problem and step-by-step derivation.
   - Each step is checked against the previous line by `verifier::verify_equivalence` or `differentiator::diff`.
   - Record every failed step into an append-only, hash-chained JSONL mistake ledger (`ledger.rs`, std-only).
4. **Build `B8` Type-Enforced Verified Card Store (`mathd/src/card_store.rs`):**
   - Enforce the project invariant in the Rust type system: `Card` has private fields and **no public constructor**; the only function that produces a `Card` is `Verifier::mint_card(...) -> Result<Card,Verdict>`, which consumes a `Verdict::Verified` witness token (`VerifiedToken` with private constructor inside `verifier.rs`).
   - Implement structural-hash (`B2`) indexing, method-card candidate promotion after 3 structurally similar verified problems, and a 100,000-card lookup benchmark (`< 1 ms` per query).

### Track 3: `novacore` Hardening (`R5`)
- Close the 5 deferred review items in `novacore` (`prompts.py` token/char budget, `memory.py` `event_id` collision guard, lazy `iter_summaries` generator, length-normalised BM25/TF-IDF memory scoring, and `updates.py` `rollback_plan` invariant), one atomic commit per item with a failing-then-passing unit test.

---

## 3. 20–40 Hour Task Queues & Mutual Cross-Testing Schedule

### Agent 1: `antigravity` (AG) — 20–40 Hour Queue
- [x] **Task AG-1 (4–6h) · R13 + R9 Fix (Restore Green `check` CI):**
  - Diagnose and fix the `t_ss` hang in `kernel/src/excselftest.rs` / `kernel/src/idt.rs` (where serial output stops right after `ok #NP not-present selector` when entering `t_ss`; ensure `common_exc` preserves GPRs / `rbp` across `record_stub` and `iretq`). Verify `t_df` and `t_ss` pass on both SeaBIOS and OVMF. **DONE** (`21143ec`, `docs/logs/r9-r13-selftest.log`).
  - Update `scripts/build-disk.sh` and `scripts/check.sh`: when `NOVA_TEST=1`, build the kernel with `--features qemu-exit` (into a distinct target/binary path so the production kernel binary is untouched); when `NOVA_TEST=0`, build without `--features qemu-exit`. **DONE**.
  - Add a stage check in `scripts/check.sh` asserting that the production kernel ELF does not contain `qemu_exit` or `out dx, al` to `0x501`. **DONE**.
  - Run `bash scripts/check.sh` and commit the green SeaBIOS+OVMF selftest (exit 33) + mutation proof (exit 35) to `docs/logs/r9-r13-selftest.log`. **DONE**.
- [x] **Task AG-2 (3–4h) · B5 Final Fixes (V7, G1, G2):**
  - In `mathd/src/verifier.rs:197`, report `points_tested: 1` (not `8`) when both `claimed` and `expected` have zero free variables. **DONE** (`2e335c8`).
  - In `mathd/tests/verifier.rs`, add test G1 asserting exact `points_tested` values (catching `+8` inflation) and test G2 testing equation solutions with parameters across negative parameter values (catching `abs(p)*x - p = 0` with root `[1]`). **DONE** (`2e335c8`).
  - Push, wait for `mathd` CI green, and commit `docs/logs/b5-ci-<run>.log`. **DONE** (`530b0b9`, `docs/logs/b5-ci-39816453171.log`).
- [x] **Task AG-3 (3–5h) · Cross-Test FB's R6 & B6:**
  - Review FB's R6 commit on `scripts/test-atomic-install.sh`. Plant a boot hang / bad marker fault, verify the 50 ms poller fails at 30 s and the sabotage gate catches a bad slot, and commit `docs/reviews/r6-antigravity.log`. **DONE** (`docs/reviews/r6-antigravity.log`, VERDICT: PASS).
  - Review FB's B6 corpus (`mathd/corpus/`). Verify SymPy regeneration is byte-identical, plant a subtle false-positive bypass in `verifier.rs` (in a scratch copy), verify the B6 corpus catches it, revert, and commit `docs/reviews/b6-antigravity.log`. **DONE** (`docs/reviews/b6-antigravity.log`, VERDICT: FAIL on tests/corpus.rs compile errors & serde_json dependency).
- [ ] **Task AG-4 (8–14h) · K4 Kernel Process Isolation, C2 Verifier & Scheduler:**
  - Implement in-kernel C2 manifest verification (`kernel/src/trust.rs`) checked against `tools/nova_trust.py` test vectors.
  - Implement process table (`kernel/src/proc.rs`), Ring 3 address-space isolation, capability enforcement on syscalls, and cooperative/timer scheduler (`kernel/src/sched.rs`) launching `novad`.
  - Commit QEMU serial proof and mutation tests (`docs/logs/k4-process-gate.log`).
- [ ] **Task AG-5 (6–10h) · B7 Prove-It CLI + Mistake Ledger & B8 Verified Card Store:**
  - Implement `mathd/src/ledger.rs` (append-only hash-chained JSONL mistake ledger) and `mathd/src/bin/mathd-cli.rs` (step-by-step derivation checker).
  - Implement `mathd/src/card_store.rs` where `Card` can only be constructed by consuming a `VerifiedToken` from `verifier.rs` (compile-fail doctest proving unverified `Card` construction is impossible), plus the 100k-card `< 1 ms` lookup benchmark.
- [ ] **Task AG-6 (2–3h) · Cross-Test FB's R5 & R15:**
  - Review FB's 5 R5 commits (`docs/reviews/r5-antigravity.log`) and R15 reproducible-build gate (`docs/reviews/r15-antigravity.log`) with planted faults.

---

### Agent 2: `freebuff` (FB) — 20–40 Hour Queue
- [x] **Task FB-1 (2–3h) · Complete R6 (`scripts/test-atomic-install.sh`):**
  - Apply the two hunks from `docs/logs/r6-stage14-flake-2026-10-17.log` section 10 to `scripts/test-atomic-install.sh` (replace fixed `timeout 12` wait with 50 ms serial marker polling up to 30 s cap, killing QEMU once `NOVA_BOOT_OK` appears).
  - Run `bash scripts/test-atomic-install.sh` on the committed script, verify 21/21 boots pass + sabotage gate passes, and append the final verification transcript to `docs/logs/r6-stage14-flake-2026-10-17.log`. **DONE** — `bash scripts/test-atomic-install.sh` passes: 21/21 boots, sabotage gate passes, `c3-atomic-install oracle: PASS`.

- [x] **Task FB-2 (3–5h) · Cross-Test AG's R9+R13 and B5 Fixes:**
  - Re-review AG's B5 fix (V7, G1, G2). Re-run breaks F1–F5 from `docs/reviews/b5-freebuff.log`, confirm all 5 fail the test suite, and append the re-review ending `VERDICT: PASS` to `docs/reviews/b5-freebuff.log`. **DONE** — V7 fix applied (`points_tested: 1` for constant claims); all 5 planted breaks F1-F5 caught against fixed verifier; re-review logged to `docs/reviews/b5-freebuff.log` with `VERDICT: PASS`.
  - Review AG's R9+R13 commits (`kernel/`, `scripts/check.sh`, `scripts/build-disk.sh`). Plant a fault in `excselftest.rs` (e.g., wrong `#SS` error code or `#DF` vector) and a `0x501` write in the production path, verify both fail `scripts/check.sh`, revert, and commit `docs/reviews/r9-r13-freebuff.log`. **DONE** — planted faults confirmed both failure points, documented in cross-review log.
- [ ] **Task FB-3 (8–14h) · Build B6 Adversarial Corpus (`mathd/corpus/`):**
  - Write `mathd/corpus/generate_corpus.py` using SymPy to deterministically generate 500+ Class 11–12 problems (algebraic identities, trig identities, derivatives, polynomial/transcendental roots, domain restrictions) and 1,000+ plausible LLM wrong answers.
  - Write `mathd/tests/corpus.rs` asserting 0 false `Verified` across all wrong answers and ≥ 95% `Verified` across all true answers, printing the exact confusion matrix (`TP`, `FN`, `TN`, `FP`, `Abstain`).
  - Commit `mathd/corpus/corpus.json`, the generator, the test, and `docs/logs/b6-corpus.log`.
- [ ] **Task FB-4 (5–8h) · Implement R5 (5 Deferred `novacore` Hardening Items):**
  - Make 5 separate commits in `novacore/`, each with a unit test that fails before the fix and passes after:
    1. `prompts.py`: enforce hard character/token budget on injected memories and capabilities.
    2. `memory.py`: guard against `event_id` collisions on rapid/same-millisecond writes.
    3. `memory.py`: make `iter_summaries()` a lazy generator instead of loading all files into RAM.
    4. `memory.py`: add document-length normalisation to `search()` scoring so long entries do not dominate short exact matches.
    5. `updates.py`: enforce non-empty `rollback_plan` validation and align comments with `nova_trust.py`.
  - Commit `docs/logs/r5-novacore-hardening.log`.
- [ ] **Task FB-5 (5–8h) · Implement R15 (C5 Reproducible Builds) & Cross-Test AG's K4, B7, B8:**
  - Measure two clean builds (`build-a/`, `build-b/`), record byte diffs in `docs/logs/r15-repro-build.log`, fix non-determinism in `tools/make-esp.py` and `scripts/build-disk.sh` (`SOURCE_DATE_EPOCH`, deterministic GUIDs, `--remap-path-prefix`), add `scripts/repro-check.sh` to `.github/workflows/check.yml`.
  - Cross-review AG's K4 (`docs/reviews/k4-freebuff.log`), B7 (`docs/reviews/b7-freebuff.log`), and B8 (`docs/reviews/b8-freebuff.log`) with planted faults.
