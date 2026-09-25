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
