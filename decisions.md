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
