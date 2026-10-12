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
