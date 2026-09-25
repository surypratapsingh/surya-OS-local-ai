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
