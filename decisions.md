# Decision Log
Format per entry:
## YYYY-MM-DD - Decision title
- Decision:
- Context:
- Alternatives rejected:
- Consequences:

Older decisions are recorded in `docs/plan-v2.md` ("Decisions locked this round"), `novacore/DECISIONS.md` and the design docs in `docs/`.

## 2026-09-22 - Independent oracles for every check (AGENTS.md)
- Decision: No test may compare a value with the source it was generated from. Oracles are external tools, or are derived from the spec with section citations.
- Context: An earlier build shipped a "✅ boots" milestone while the boot log showed a failure. The checkers shared their subject's assumptions.
- Alternatives rejected: Self-written checkers based on the implementation.
- Consequences: The disk verifier is written from UEFI 2.10 §5.3 and is meant to be backed by sgdisk/fsck.fat/mdir; mathd is meant to use SymPy, finite differences and mutation testing. (Corrected 2026-09-24: neither is true yet. The oracles have never run, and mathd has no SymPy fixtures and does not compile. See progress.md.)

## 2026-09-22 - Pendrive installer as first deliverable
- Decision: Ship `build/nova.hdd` (BIOS+UEFI) first.
- Context: Plugging in the stick is the product moment.
- Alternatives rejected: Starting from the AI layer.
- Consequences: The disk-image tooling is pure Python and needs no mtools/xorriso, so it runs on Windows.

## 2026-09-22 - Minimal Linux as daily driver, Nucleus later
- Decision: Use a minimal Linux base for drivers now; the custom kernel converges later (K6: mathd runs unchanged on Nucleus).
- Context: A from-scratch kernel can't drive real hardware for years.
- Alternatives rejected: Nucleus-only from day one.
- Consequences: Two tracks (A kernel, B mathd) run in parallel.

## 2026-09-22 - Promote W5 above log work
- Decision: The owner moved "keep owner content off the disk" ahead of tamper-evident logs.
- Context: novacore is usable today, so the privacy exposure was live.
- Alternatives rejected: Original W4→W6 order.
- Consequences: Three llm.py items (temp-file leak, subprocess env, unbounded stdout) fixed in W5.

## 2026-09-25 - Ed25519 in pure standard-library Python, OpenSSL as oracle only
- Decision: `tools/nova_trust.py` implements Ed25519 from RFC 8032 §5.1. OpenSSL (via `cryptography`) is used only by the dev-only fixture generator `tests/fixtures/gen_ed25519_openssl.py`.
- Context: AGENTS.md rule 12 requires the Python tools to use only the standard library. C1 had added `cryptography` without approval.
- Alternatives rejected: keeping `cryptography` or PyNaCl as a runtime dependency; copying the RFC 8032 §6 reference code.
- Consequences: not constant-time, so signing happens only on the offline machine. Each key-sign-verify cycle takes about 11 ms. Correctness rests on byte-for-byte agreement with OpenSSL plus a mutation run.

## 2026-09-25 - One signing path: manifests only
- Decision: Removed `sign-release.py`, `verify-release.py` and `prepare-release.py`. The C1 ceremony now ends in `sign-manifest.py`.
- Context: C1 had introduced a second signed format (a "release hash" text file) that duplicated manifests.
- Alternatives rejected: keeping both formats.
- Consequences: one format to specify, test and audit.

## 2026-09-25 - Manifest v1 fails closed
- Decision:
  - Unknown fields are errors.
  - Capabilities are exact names with no wildcards.
  - File names are bare, with no path parts.
  - The signed bytes carry a `NOVA-MANIFEST-v1` domain prefix.
  - The key fingerprint hashes the raw key, not the PEM file.
  - A missing replay-state file is an error, not an implicit 0.
- Context: AGENTS.md forbids ambient authority. Git's CRLF conversion changes PEM file bytes. Deleting a state file must not reopen old releases.
- Alternatives rejected: lenient parsing; `ns:*` wildcards; fingerprinting the file; defaulting a missing state to 0.
- Consequences: a first install must create `{"release_sequence": 0}` on purpose. Replay protection is only as strong as the storage holding the state file (still open, under C3/C6).

## 2026-09-26 - Boot chain verification via Limine's path#blake2b, not Ed25519
- Decision: Limine verifies bootloader and kernel using `path#blake2b` file hashes in its config, not by interpreting Ed25519 signatures. The manifest signs the expected hashes; Limine compares them.
- Context: Limine 12.9.0 supports `path#blake2b` but not Ed25519 manifest verification. The old C4 design assumed Limine could verify manifest signatures, which it can't. This design uses what Limine actually has.
- Alternatives rejected: Modifying Limine to understand Ed25519; asking Limine to load and parse a JSON manifest; using RSA or another scheme Limine might support.
- Consequences: The manifest's `bootchain` section lists hashes; Limine's config must match them or Limine halts. Config integrity is not solved here (belongs to C6). The kernel later verifies modules (K3/K4).

## 2026-10-09 - CI fails on any SKIP
- Decision: `scripts/check.sh` exits 1 when any stage skipped and `CI=true` (set by GitHub Actions). Local runs keep the "PASSED WITH N SKIPPED" verdict with exit 0.
- Context: stages 11-13 (capability, shell, ELF64 oracles) skipped in every CI run because their QEMU check used `[ -f "$QEMU_BIN" ]` on a bare PATH name. The run still exited 0 on skips, so nothing flagged it. Review of 2026-10-08 found zero CI passes for those oracles in 24 retained runs.
- Alternatives rejected: keep exit 0 on skips in CI (hides dead oracles); a per-stage allowlist of acceptable skips (no stage needs one today; add it only when one does).
- Consequences: CI must install every tool an oracle needs. A missing tool now turns CI red instead of silently shrinking coverage.

## 2026-10-09 - FAT32 corpus must obey the spec's FAT-type rule
- Decision: the K3 test corpus must be a real FAT32 volume: at least 65525 clusters, and a FAT large enough for all of them. The kernel driver should refuse anything else. The earlier reading, that 65525 is a "formatter heuristic" (comments in `tools/gen-fat32-corpus.py` and `kernel/src/fat.rs`), is retracted.
- Context: the committed corpus has 1014 clusters and a 512-entry FAT. CI `fsck.fat` reports "1014 clusters but only space for 510 FAT entries"; mdir lists nothing; stage 9 never passed. `tools/verify-fat32.py` checked the generator's constants, not the spec, so it passed the bad image.
- Alternatives rejected: keep the small corpus and drop or soften the mdir comparison (removes the only external oracle; tried in 52cb30c, reverted); add FAT12/16 support so the small image is "valid" (outside K3 scope, and real pendrives are FAT32).
- Consequences: corpus grows to about 34 MB (1 sector per cluster is the smallest valid layout). It must still fit the 39 MiB ESP, and boot time must be measured because both limine configs load it as a module. Tracked as work order R2 in `docs/work-orders.md`.

## 2026-10-09 - Only the selftest image carries the FAT32 corpus
- Decision: `kernel/limine.conf` no longer loads `/fat32-corpus.img` as a module, and `tools/make-esp.py` puts the corpus on the ESP only when the config has a `module_path` line. `kernel/limine-selftest.conf` still loads it for the fat gate.
- Context: only `kernel/src/fatselftest.rs` reads the module, and it runs only with `novatest`. The real FAT32 corpus is 34336768 bytes; loading it cost SeaBIOS ~1.8 s per boot (4.34 s to ~6.1 s, `docs/logs/r2-step4-boottime.log`). The regular image is the one the owner boots and the one stage 14 installs onto.
- Alternatives rejected: keep the module in both configs (every real boot pays for a test fixture); shrink the corpus (below 65525 clusters it is not FAT32, see the entry above).
- Consequences: the regular ESP uses 638 of 19912 clusters again; the selftest ESP uses 17404. The regular boot report no longer prints `cmdline: fatcorpus=...`. OVMF boot time (9.7-11.5 s locally) did not measurably change, so this does not fix R6.

## 2026-10-09 - Real mdir output, not a reading of dir.c, defines the kernel's listing; break test runs every CI
- Decision: `list_dir_mdir` (kernel/src/fat.rs) and the fatselftest line probes take their layout from real mdir output in committed CI logs. The mdir-format renderer in `tools/verify-fat32.py` is deleted. Stage 9 renames one file in a corpus copy on every run and requires `DIFFER` (sabotage gate).
- Context: CI 37941316726 ran fsck.fat and mdir on the real FAT32 corpus for the first time. The kernel renderer, the Python renderer and the probes were all written from the same reading of mtools dir.c, and all had the same four layout errors. fsck.fat also found three generator bugs (LFN checksum byte, '..' cluster, boot-sector label) that the kernel and verify-fat32.py both passed, because neither actually compared the LFN checksum to the short name.
- Alternatives rejected: Fixing the Python renderer too (a third copy of the same guess, compared to nothing). A one-off manual break test on a branch (proves the diff once; the gate proves it on every run).
- Consequences: The kernel listing can only be confirmed in CI (no mtools on the Windows host). If mtools changes its layout, stage 9 fails and the renderer must follow the new real output.

## 2026-10-11 - Unsupported ticks are unticked with a dated note, not rewritten
- Decision: When the R3 audit found a ✅ or [x] without a committed log, the mark was changed (✅ to 🔨, [x] to [ ]) and a dated note was added that names the missing evidence. The original text stays. Where the tick has evidence but the text was wrong, the tick stays and a note names the wrong details and the log that now backs the claim. CI runs cited only by number were committed as verbatim excerpts with the gh command and line range in a header.
- Context: Rule 15 (status and logs must agree) and the user rule "never delete history". M0 was unticked because W3 defines it: "M0 is not ✅ until this table exists and every row passes".
- Alternatives rejected: rewriting the roadmap paragraphs to the current state (loses the record of what was claimed and when); leaving CI runs cited by number only (a run's log on GitHub can expire, and the reader cannot grep it).
- Consequences: roadmap.md and plan.md get longer. Each note points at `docs/logs/status-audit-2026-10-11.log`. Re-ticking needs the log named in the note.

## 2026-10-12 - R8 evidence comes from a harness written from the work-order text; gaps block the re-tick
- Decision: The W4, W5 and W6 transcripts come from `tools/novacore-evidence.py`, which applies each work order's done-when and mutation list as written in `docs/work-orders.md`, not the novacore unit tests. Where a transcript shows a gap, W4, W5 or W6 stays unticked and the gap becomes a work order (R10, R11, R12). The novacore suite runs inside `scripts/check.sh` stage 4 (host tests), not as a new stage.
- Context: R8 found that several novacore tests cannot fail on the case they name. The W5 canary scanner reads at most 5 subdirectories and 20 files per directory, and two of the three canary tests never scan for the token. The W6 "stale chain link" test uses a chain hash of 64 zeros. A transcript made by running those tests would repeat their weakness (AGENTS.md rule 1).
- Alternatives rejected: Producing the transcripts from the existing tests (they share the implementation's blind spots). Fixing the tests and code inside R8 (that is W4-W6 rework, a different work order; AGENTS.md rule 11). A new check stage 15 (every doc, log and the CI excerpts cite "14 stages"; stage 4 already runs the other host tests).
- Consequences: W4, W5 and W6 stay unticked until R10, R11 and R12 land. The harness is a second implementation of the chain format (memory.py docstring), so a change to that format must update the harness. The W5 scan reads every file in the system temp directory, which takes minutes on this host.

## 2026-10-12 - The canary scanner skips non-regular files (owner decision)
- Decision: `_scan_for_token` in `novacore/tests/test_llm_privacy.py` and `scan` in `tools/novacore-evidence.py` open regular files only (`Path.is_file()`). The harness counts every skipped entry as unreadable.
- Context: CI run 38285668806 hung for 24 minutes in `test_no_prompt_leak_to_system_temp_via_stdin` and was cancelled (`docs/logs/ci-38285668806-stage4-hang.log` lines 1033-1034). The scanner opens every file in `/tmp`, and opening a FIFO for reading blocks until a writer appears. The likely FIFOs are the .NET runner's `clr-debug-pipe-*`. This is not verified: there is no Linux host here. AGENTS.md rule 7 applies (the test is wrong, not the code), so the owner chose the fix.
- Alternatives rejected: Removing the novacore line from `scripts/check.sh` until R11 (CI would not run the suite, and R8 asks for exactly that). Leaving main as it was (every push would hang CI until the 6-hour job limit).
- Consequences: A token in a FIFO, socket or device is not found. None of those keeps data after the reader is gone. On Windows no file changes category, so the committed W5 counts are unchanged. The scanner's other limits (5 directories, 20 files) are still R11 work.

## 2026-10-12 - Three agents share one folder, coordinated by file claims on a team board
- Decision: The owner made Claude Code the lead over Claude Code, Google Antigravity and Freebuff. `docs/team.md` assigns each agent one work order and the files it may write. Every commit is made with `git commit -- <paths>` and names its agent. A work order is done only after a review by a different agent that includes a deliberate break, logged in `docs/reviews/`. Reviews go in a cycle (antigravity checks claude, freebuff checks antigravity, claude checks freebuff). Only the lead ticks status files.
- Context: On 2026-10-12 two agents worked on the R8 CI hang at once. One agent's uncommitted test fix was undone, and a log was committed with mojibake. Nothing said who owned what.
- Alternatives rejected: One git worktree and branch per agent (better isolation, but each tool would need to reopen a different folder, and the uncommitted B3 work lives in this tree; adopt it if claims keep colliding). No coordination (that is what caused the clash). Pairwise reviews (two agents could share a blind spot for good).
- Consequences: Agents must read `docs/team.md` before each task. The shared index stays a risk; `git commit -- <paths>` covers commits, not `git checkout` or `git stash`, so rule 2 on the board forbids those on others' files.

## 2026-10-12 - W4 property 1 covers every CATALOGUE entry, not only confirmation-required ones (freebuff, R10)
- Decision: The prompt property test asserts that every capability in `CATALOGUE` is named in `SYSTEM_PROMPT`, and that `SYSTEM_PROMPT` names no dotted capability-like token outside an explicit vocabulary list in `novacore/tests/test_prompts.py`. The test also requires each vocabulary name to exist in `CATALOGUE`, so the two can never drift in either direction.
- Context: R10's probe 2 adds a `requires_confirmation=False` capability that is not named in the prompt, and the suite must fail on it. The weaker reading of W4 item 4 (only confirmation-required capabilities must be named) would let probe 2 pass. The prompt therefore now names all seven catalogue entries (`system.status`, `screen.say`, `audio.speak` were previously unnamed). The vocabulary list is pinned explicit on purpose: an addition to either side fails the suite, which is how prompt/code drift becomes a CI failure.
- Alternatives rejected: Confirmation-required-only scope (probe 2 would pass); regex-only checks without a vocabulary list (a rename or a new invented name in prose could slip past); checking substring presence per name without set equality (detects drift only in one direction).
- Consequences: Adding any capability to `CATALOGUE` now requires naming it in `SYSTEM_PROMPT` and adding it to the test vocabulary — three places, all caught by CI if any is missed. `_test.stub` was removed from the production `CATALOGUE` (a test fixture does not belong in the security boundary); `novacore/tests/test_capabilities.py` injects it with `unittest.mock.patch.dict`. Note for the lead: `tools/novacore-evidence.py w6` calls `broker.request("_test.stub", ...)` against the production catalogue — from this commit that returns `denied` and writes no events, so the w6 outbox probe needs its own injected catalogue (R12 rework or a harness edit only the lead can make).

## 2026-10-13 - The memory chain tail is anchored by a sidecar file; v1 records are refused (claude, R12)
- Decision: `MemoryStore` keeps `memories.jsonl.anchor` next to the log, holding the record count and the last `chain_hash`. `add()` rewrites it atomically (temporary file, then `os.replace`) after each append. `all()` fails on a count or head mismatch, on a missing anchor while the log has records, and on a missing log while the anchor exists. `add()` raises on a broken log instead of starting a new chain. The read cache is keyed on (mtime_ns, size) of the log and of the anchor. `anchor()` is the migration for logs that have none. v1 records (no `chain_hash`) are refused with their line number, with no migration.
- Context: R8 found that dropping the last records at a record boundary passes (`docs/logs/r8-w6-tamper.log`, mutation 6b). A chain with no anchor accepts any valid prefix. R12 asks for an anchor the owner can check.
- Alternatives rejected: A keyed MAC (the key would have to live in the same folder, which gives nothing against someone who can write the folder; a key the owner holds elsewhere needs a key ceremony that does not exist yet). A header line in the log holding the count (anyone can rewrite it as easily as a sidecar file, and it changes the record format that the docs and the harness describe). Migrating v1 records into the chain (it would vouch for an order and timestamps that nobody verified).
- Consequences: Every existing log without an anchor is refused until the owner runs `anchor()` once (command in `novacore/SCOPE.md`). The anchor does not stop anyone who can write the folder; `SCOPE.md` says so. A crash between the append and the anchor write leaves every read failing closed until the owner re-anchors.

## 2026-10-13 - The outbox shares the memory log's chain and anchor; a broken outbox raises (claude, R12)
- Decision: The chain format and the tail anchor live in one module, `novacore/src/novacore/chainlog.py`, used by `MemoryStore` and `CapabilityBroker`. `CapabilityBroker.request()` verifies the whole outbox (JSON, `sequence` counting up from 1, chain, anchor) before it appends, raises `ValueError` naming the first bad line, and leaves the file unchanged. It writes `events.jsonl.anchor` after each append. `CapabilityBroker.anchor()` is the migration for an outbox that has none.
- Context: R8 found that `_read_last_sequence_and_chain` read only the last line and restarted at sequence 1 on a bad one (`docs/logs/r8-w6-tamper.log`). R12 says a broken outbox must raise.
- Alternatives rejected: Returning `DispatchResult("denied", ...)` instead of raising (keeps the W4 return type, but a caller that only checks `state` could carry on as if the outbox were fine; R12 asks for a raise, and `cli.py` already turns `ValueError` into an error message and exit code 2). A second copy of the anchor code in `capabilities.py` (two copies drift; the harness is already a second implementation of the format).
- Consequences: Every existing `events.jsonl` with events is refused until the owner runs `CapabilityBroker(path).anchor()` once (command in `novacore/SCOPE.md`, "Log integrity"). `request()` can now raise `ValueError` for capabilities that write an event; the denied, confirmation-required and unavailable paths do not read the outbox. Each write reads the whole outbox, so a write costs time proportional to its length; no outbox benchmark was taken.

## 2026-10-15 - Two-agent review while freebuff is away; antigravity takes R10 and R11 (lead)
- Decision: The owner said freebuff is busy elsewhere. Its claims (R10 fixes, R11) move to antigravity. claude reviews antigravity's work (B3, R10, R11, B4) and antigravity reviews claude's (R5 next). Each reviewer must break the subject in a way the author did not already try.
- Context: The three-way review cycle in `docs/team.md` needs freebuff. B3 had been waiting for freebuff's review since 2026-10-12, and R10 had been waiting for freebuff's fixes.
- Alternatives rejected: Waiting for freebuff (blocks W4, W5 and B3 for an unknown time). claude doing R10 and R11 itself (claude is R10's reviewer, and the owner asked for more work to go to antigravity).
- Consequences: Two agents now check only each other, which is the blind spot the cycle was meant to prevent. The rule that each reviewer tries a new break is the partial guard. Restore the cycle when freebuff returns.

## 2026-10-15 - Non-finite variable bindings map to EvalError::Domain (antigravity, B3)
- Decision: `eval_at` rejects non-finite variable bindings (`NaN`, `+inf`, `-inf`) with `EvalError::Domain { arg }` carrying the non-finite float value. `check_finite` also maps `NaN` to `EvalError::Domain { arg }` (preserving `Overflow` for `±inf`).
- Context: Finding F1 in `docs/reviews/b3-claude.log`. Probe results showed `x` with `x = NaN` returned `Ok(NaN)` and `x` with `x = inf` returned `Ok(inf)` because `Variable` and `UnaryOp` arms did not check finiteness. Furthermore, `check_finite` mapped `NaN` to `Overflow`, misdiagnosing `x + 1` with `x = NaN`.
- Alternatives rejected: Adding a new enum variant `EvalError::InvalidBinding` (would break exhaustive matches in existing consumers and wasm harnesses without adding conceptual clarity: non-finite values lie outside the real domain $\mathbb{R}$ on which `mathd` operates); mapping `NaN` to `EvalError::Overflow` (incorrectly names an overflow fault when no arithmetic capacity was exceeded).
- Consequences: All callers passing non-finite values to expressions receive `EvalError::Domain { arg }`. Neither `NaN` nor `±inf` can escape `eval_at` as a value or through unhandled unary operations.

## 2026-10-15 - B3 fixtures computed at 35 digits in mpmath and stored as nearest f64 in JSON (antigravity, B3)
- Decision: Work order B3's specification ("at 30 digits of precision... The Rust test reads the JSON and asserts agreement within 1e-10 relative error") is implemented by calculating oracle values at 35 decimal digits of precision in mpmath, and storing each result as the nearest IEEE-754 binary64 (`f64`) float in JSON.
- Context: Finding F5 in `docs/reviews/b3-claude.log`. The previous wording ("precision_digits": 30, "expected values carry >= 30 digits") was ambiguous about whether the JSON file itself held 30-digit strings or `f64` numbers. The 53-bit mantissa of IEEE-754 `f64` provides ~15-17 significant decimal digits.
- Alternatives rejected: Storing multi-precision 35-digit strings in JSON and implementing a custom arbitrary-precision decimal evaluator in `mathd` (work order B3 explicitly specifies `eval_at(&Ast, &HashMap<String, f64>) -> Result<f64, EvalError>` with a 1e-10 relative error gate; storing the exact binary representation as `f64` ensures zero representation error across the JSON/Rust boundary and provides ~5-7 orders of magnitude of safety margin beyond the 1e-10 gate).
- Consequences: Fixture JSON file values are parsed directly as native `f64`. Relative errors between `mathd` and mpmath are evaluated at `1e-10` relative tolerance for all nonzero numbers, and exact zero for zero values.

## 2026-10-16 - R9: qemu_exit goes behind a cargo feature, off by default (lead, delegated by the owner)
- Decision: Build W1 item 3 as written. `kernel/Cargo.toml` gets `[features] qemu-exit = []`, off by default. Only the selftest image is built with `--features qemu-exit`. Without the feature, `qemu_exit::success()` and `qemu_exit::failure()` print their marker and halt; they never write the isa-debug-exit port. The proof is a check, in CI, that the production kernel binary has no `out` to port 0xf4 (or no `qemu_exit` symbol), plus the usual selftest exit 33 and a mutation that shows exit 35. Also in R9: fix the citations in `kernel/src/limine.rs:4` and `kernel/src/pmm.rs:5` and delete or replace the 14-byte `.freebuff/ref/PROTOCOL.md` in its own commit. Assigned to freebuff; reviewer claude.
- Context: The owner delegated R6 and R9 to the lead on 2026-10-16 ("find the best available method and fix them"). W1 has been unticked since 2026-10-11 because the exit is gated at runtime by the `novatest` command line (`plan.md`, W1 line; `docs/work-orders.md` R9).
- Alternatives rejected: Accepting the runtime gate. The exit port write would ship in every production kernel, reachable by anyone who can edit the Limine command line on the pendrive. The invariant "capabilities are explicit" treats a test hook in a production binary as ambient authority.
- Consequences: CI builds two kernels (selftest with the feature, production without). The C4 bootchain hashes cover the production kernel, so stage 5 must still pass on the new build. W1 re-ticks when R9's review ends `VERDICT: PASS`.

## 2026-10-16 - R6: measure first; take the countdown out of test images, keep the 12 s limit (lead, delegated by the owner)
- Decision: Run the R6 steps first: 10 runs of `scripts/test-atomic-install.sh` and one timed OVMF boot to `NOVA_BOOT_OK`, committed as `docs/logs/r6-stage14-flake-<date>.log`. Then, if the stage 14 images use a Limine config that only tests build, set `timeout: 0` there and keep `timeout 12` in `boot_check`. If stage 14 boots the production install config (the one the C4 bootchain hashes cover), do not change production boot behaviour for a test: raise `timeout` instead, to a value with a measured reason written next to it. Either way stage 5 (bootchain hashes) must still pass. Assigned to freebuff after R9; reviewer claude.
- Context: R6 offered the owner two options: a longer timeout with a measured reason, or `timeout: 0` in the test images' Limine config. The owner delegated the choice on 2026-10-16. The 3 s countdown does nothing in a test, and the failure was two OVMF boots at rc=124 in one local run (`docs/logs/review-2026-10-09-check-local.log`).
- Alternatives rejected: Raising the timeout with no measurement (AGENTS.md rule 8). Changing the production Limine countdown to make a test faster (a test should not change what the owner sees at boot).
- Consequences: Test images boot about 3 s faster. If the measurement shows boots near the limit even without the countdown, that is a finding for the lead, not a reason to raise the limit quietly.

## 2026-10-16 - K2 gate ticked with 23 of 32 vectors fired by software int n (lead)
- Decision: Tick the K2 gate in `plan.md`. The done-when asks for every one of the 32 vectors to be "deliberately triggered by a test" and to report correctly. 9 are raised natively by the CPU with asserted error codes and CR2. All 32 are dispatched through their real IDT gates by `int n`. #XM is a loud SKIP natively under QEMU TCG (qemu#215) and passes through `int n`. Follow-up, not blocking: raise #SS natively (non-canonical stack access) and #DF natively, and #AC once ring 3 exists in K4.
- Context: The line read "[ ] K2 gate ... (✓ done 2026-09-26)" since September. The R3 audit called the gate log one serial capture with no firmware or exit code. CI run 38739899543 (2026-10-15, `docs/logs/r11-ci-38739899543.log` lines 1258-1275) shows the selftest image exit 33 on SeaBIOS and OVMF; `kernel/src/main.rs` lines 125-138 run the exception gate first and exit 35 on any failed check. `kernel/` has not changed since 4e6a6a4 (2026-10-12). The mutation proof is `docs/logs/k2-exception-gate-mutation-proof.log`.
- Alternatives rejected: Leaving it unticked until every vector is raised natively. #MC, the reserved vectors and #XM under TCG have no raisable condition in this VM, so that bar could never be met here.
- Consequences: 12 of 34 milestones are ticked. `docs/roadmap.md` says #DF has "no VM-raisable condition"; that is too strong (a fault while delivering a fault raises it), so the native #SS/#DF follow-up goes on the kernel queue.

## 2026-10-16 - Review cycle restored; agent queues set by the critical path (lead)
- Decision: freebuff is back (board, 2026-10-15 10:02 and 18:25). The cycle is restored: antigravity checks claude, freebuff checks antigravity, claude checks freebuff. freebuff reviews B4 now; claude does a lead check after. Queues: antigravity does the R11 and R10 fixes, then B4 fixes, then B5, then moves to the kernel (K4). freebuff does the B4 review, R9, R6, then builds the B6 corpus in `mathd/corpus/` while B5 is in progress. claude reviews, keeps CI and the board, and drafts D5 (threat model).
- Context: The kernel rungs K4-K7 are the critical path (about 180 of the likely 250 remaining days in the lead's estimate). Nobody holds `kernel/`. mathd will run out of work for one agent after B5.
- Alternatives rejected: Keeping the two-agent review (the blind spot recorded on 2026-10-15). Putting freebuff on the kernel at once (R9 and R6 are smaller kernel-adjacent jobs that unblock W1 and C3's margin first).
- Consequences: B6 and B5 run at the same time, so `mathd/` is split: freebuff holds `mathd/corpus/` and its generator, antigravity holds the rest. B6 can only finish after B5.

## 2026-10-16 - W4 and W5 ticked with two prompt-test gaps left open; B4 held on its tolerance rule (lead)
- Decision: Tick W4 and W5. R10's second re-review and R11's re-review end `VERDICT: PASS`. Two gaps found in the R10 re-review do not block: F7 (a hyphenated name such as `camera.enable-all`) and F8 (a one-letter last part such as `system.x`) still pass `novacore/tests/test_prompts.py`. B4 stays unticked: the lead check FAILs on L1, and L1 blocks.
- Context: The W4 done-when is a demonstrated prompt-drift failure; the harness prints 4 of 4 probes caught. F7 and F8 sit in the pattern the reviewer suggested, not in a fix the author got wrong. The broker refuses any name not in its catalogue (`novacore/src/novacore/capabilities.py` line 152, `CATALOGUE.get(name)`; `test_unknown_capability_is_denied`, `novacore/tests/test_capabilities.py` line 76), so a drifted name in the prompt cannot grant anything. For B4, diff.json states a strict 1e-10 relative tolerance and the test applies 1e-10 * max(1, |want|), so 18 cases check nothing. B3 F2 and F3 were the same class and were blocking.
- Alternatives rejected: Holding W4 until F7 and F8 are fixed (the done-when is met, and the broker covers the gap). Passing B4 with L1 as a note (a stated rule that differs from the applied rule is the failure `AGENTS.md` was written about).
- Consequences: antigravity's queue: B4 L1, the R10 follow-up for F7 and F8, the D5 review, then R14. freebuff reviews B5 before R9, because B6 cannot finish without B5; then R9, R13, R6, B6, R15. claude re-checks B4, does the B5 lead check, reviews R9, then does R5. Threat-model rows T7, T9, T10 and T19 now cite the re-reviews. T19 records a code-read finding: `memory.py` line 76 and `capabilities.py` line 189 set no file mode, so on POSIX those files follow the umask. No planted fault was run on POSIX.

## 2026-10-17 - R9 and R13 move from freebuff to antigravity; D2 waits for the CLI path (lead)
- Decision: Give R9 and R13 (the kernel jobs) to antigravity. freebuff keeps the B5 review, R6, B6 and R15. The lead does the B5 lead check now instead of after freebuff's review; freebuff's review is still required before B5 is ticked. B4 is ticked. D2 is not ticked although R14's review passed.
- Context: freebuff's last message is 2026-10-15 21:15, and no commit since then touches `kernel/`. antigravity finished four items on 2026-10-16 (B4 L1, the R10 follow-up, the D5 review, R14). The kernel rungs are the critical path (`decisions.md` 2026-10-16, "Review cycle restored"). R14's test uses a fake runner and never calls the CLI, as the lead's work order said; a socket planted in `LocalCommandBackend.complete` or `cli._runtime` passes every test (`docs/reviews/r14-claude.log`, G1). Every real session runs through those two functions.
- Alternatives rejected: Waiting for freebuff (the kernel would stay idle with no end date). Moving the B5 review to antigravity (it wrote B5). Ticking D2 with the limit written beside it (the untested path is the normal one).
- Consequences: antigravity writes `kernel/` under R9 and R13 and does the R14 G1 follow-up first. The cross-check cycle has a gap while freebuff is away: claude reviews everything antigravity writes, and nobody reviews claude's D5 edits except antigravity. If freebuff returns, it reviews B5 first.

## 2026-10-17 - B5 fix goes before R9 in antigravity's queue (lead)
- Decision: antigravity fixes B5 findings V1, V2, V3, V5 and V6 before starting R9; R14 G2 comes second.
- Context: the B5 lead check (`docs/reviews/b5-claude.log`) found that the verifier returns Verified for claims that are wrong only for x < 0. The work order calls a false Verified a project-stopping bug, and every stored answer will rest on the verifier. R9 is a kernel test-harness feature with no dependants waiting.
- Alternatives rejected: keep R9 first and fix B5 later (leaves a known false Verified in the one component the project rests on); give the B5 fix to freebuff (silent since 2026-10-15 21:15; freebuff is also B5's reviewer, so the author should fix).
- Consequences: R9, R13 and K4 move back by one work order. B6 still waits for B5. The owner must answer V4 (does Verified mean "every claimed root is a root" or "the full solution set") before `verify_equation_solution` changes.

## 2026-10-30 - B5-V8: a point where only one side is defined is a counterexample (lead default, owner may override)
- Decision: In the verifier, if one side of a claim evaluates to a finite value at a sample point and the other side does not, the claim is Failed at that point. If neither side is defined, the point is skipped as before. The V8 fix goes to freebuff, not antigravity.
- Context: B6's domain arm found `ln(x^2) = 2*ln(x)` and three similar claims Verified, because `mathd/src/verifier.rs:285-287` and `:261-263` skip any point where either side errors. The negative half-line is thrown away. antigravity holds the P0 kernel K4 fix and has been silent since 2026-10-24; freebuff is otherwise blocked.
- Alternatives rejected: judge identities on the common domain only (keeps the false Verified; the owner can still choose it, see V4 and V8 in `docs/reviews/b6-fix-1030-claude.log`); leave the 4 rows reported but unscored (a known false Verified in the component the card store depends on); give V8 to antigravity (blocks K4 further).
- Consequences: strict can only reject more. Some SymPy-true corpus rows may be rejected if the two sides have different natural domains; freebuff reports these and stops, it does not exempt them. `Verdict` may change shape, which touches antigravity's B7 files; freebuff posts first. The domain arm in `tests/corpus.rs` becomes 17/17 and the `expressible` flag goes.
- **Owner confirmed 2026-10-30:** strict rule stands. A `Verdict::FailedDomain` variant carries it (approved in `docs/team.md` 2026-10-30 06:40); freebuff may add its two arms in `mathd/src/bin/mathd-cli.rs` for V8 only.
- **Consequence found 2026-10-30 (`docs/reviews/v8-precheck-claude.log`):** under the strict rule, 4 claims in the B5 fixture that were marked true are false over the reals (`ln(x*y) = ln(x) + ln(y)`, `ln(x/y) = ln(x) - ln(y)`, `ln(x^2) = 2*ln(x)`, `sqrt(x)*sqrt(y) = sqrt(x*y)`). They become must-reject, and the pinned true count goes from 323 to 319. B6 domain-015 `ln(x^3) = 3*ln(x)` is true under the same rule and becomes a control row. The NOVA meaning of "true" is now: equal at every real point where either side is defined.

## 2026-10-31 - The owner's assistant track: model text may propose a catalogue action (owner)
- Decision: The owner asked for a personal assistant that does PC tasks by chat or voice command, with the best model that runs locally. Phase N is added to `docs/work-orders.md`. In N1 a router model call turns the owner's words into a *proposal* for one catalogue action; the broker re-validates it and a narrow executor on the host runs it. N1 goes to the lead; antigravity reviews.
- Context: `novacore/src/novacore/cli.py` said "Model text is never parsed into commands or capabilities" (W4). The reason was prompt injection: recalled notes and imported files are untrusted. N1 keeps that threat closed by structure, not by the prompt: the router prompt is built from the owner's words and the action list only; the runner's output is grammar-limited and then re-parsed strictly; a confirmation comes only from the owner's next typed line; executors take validated arguments only (no shell, absolute program paths, owner-listed folders, document and media types only).
- Alternatives rejected: Parsing the chat model's reply for actions (the chat prompt carries untrusted notes and history). Waiting for Nucleus K4-K7 before any action (the owner wants it on the daily driver now, and `docs/plan-v2.md` already puts the daily driver on a host OS). A model trained from scratch in this repository (considered and dropped before any code was written: it could not be "the best model that runs locally"; plan v2 has intelligence arrive as swappable weights).
- Consequences: The W4 boundary wording changes in `cli.py`, `novacore/SCOPE.md` and `SYSTEM_PROMPT`; every new capability is named in the prompt and the test vocabulary (the 2026-10-12 three-place rule). The model choice is N3 and needs the owner's RAM and GPU. Nothing in N1 is measured against a real model: llama.cpp was not built or run in this session.
