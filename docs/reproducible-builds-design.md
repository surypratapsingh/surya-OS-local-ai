# Reproducible Builds — C5 Specification

Identical inputs produce byte-identical image, proven in CI.

---

## Goal

Anyone can rebuild NOVA from source and verify they get the exact same binary as the official release. This proves no supply-chain tampering occurred.

---

## Strategy

1. **Deterministic build system** — No timestamps, random seeds, or non-deterministic tools
2. **Locked dependencies** — Cargo.lock, pinned Limine version, known compiler version
3. **CI verification** — Build on CI, compute hashes, compare to manifest
4. **Public proof** — Hash comparison results published, auditable

---

## Build Determinism Checklist

| Component | Requirement | How |
|-----------|-------------|-----|
| **Compiler** | Fixed version | `rustc 1.75.0` in CI config |
| **Dependencies** | Locked | `Cargo.lock` committed, not updated |
| **Timestamps** | Removed | Set `SOURCE_DATE_EPOCH=0` in build |
| **Build paths** | Reproducible | No `/tmp/random-build-123` paths in binaries |
| **Linker** | Deterministic | `ld` with fixed seed, no ASLR during build |
| **Toolchain** | Exact version | `.toolchain-version` file in repo |

---

## CI Verification Workflow

```
GitHub Actions (on release):

1. Checkout tagged commit
   ↓
2. Build kernel with SOURCE_DATE_EPOCH=0
   ↓
3. Compute sha256 of build/nova-kernel-1.0.0.bin
   ↓
4. Build Limine with SOURCE_DATE_EPOCH=0
   ↓
5. Compute sha256 of build/limine-12.9.0.bin
   ↓
6. Build mathd with SOURCE_DATE_EPOCH=0
   ↓
7. Compute sha256 of build/mathd-1.0.0.bin
   ↓
8. Download manifest from owner
   ↓
9. Extract hashes from manifest
   ↓
10. Compare built hashes to manifest hashes
    ├─ All match → ✓ REPRODUCIBLE
    └─ Any differ → ✗ NOT REPRODUCIBLE (halt, investigate)
    
11. Publish verification report
```

---

## Manifest Integration

Manifest now includes build metadata:

```json
{
  "release": {"version": "1.0.0"},
  "build_info": {
    "toolchain": "rustc 1.75.0",
    "timestamp_epoch": 0,
    "build_machine": "ubuntu-22.04",
    "build_commit": "f2aea5e..."
  },
  "bootchain": {
    "limine": {"sha256": "abc123..."},
    "kernel": {"sha256": "def456..."}
  },
  "packages": [...]
}
```

---

## Proof of Reproducibility

Document in `REPRODUCIBILITY.md`:

```markdown
# NOVA Reproducible Build Report

## Release 1.0.0

| Component | Expected Hash | Rebuilt Hash | Match |
|-----------|---------------|--------------|-------|
| Limine 12.9.0 | abc123... | abc123... | ✓ |
| Kernel 1.0.0 | def456... | def456... | ✓ |
| mathd 1.0.0 | ghi789... | ghi789... | ✓ |

**Verdict:** Fully reproducible. Release can be independently verified.

**Verification date:** 2026-09-22
**Verified by:** CI/CD pipeline
**Build environment:** ubuntu-22.04, rustc 1.75.0
```

---

## Non-Determinism Sources & Fixes

| Source | Problem | Fix |
|--------|---------|-----|
| **System time** | `__FILE__`, `__DATE__` in binaries | `SOURCE_DATE_EPOCH=0` |
| **Compiler cache** | Different cache states | Clean build every time |
| **Filesystem order** | Directory listings not ordered | `sort -R` avoided, explicit ordering |
| **ASLR** | Address randomization in binaries | Disable ASLR during build |
| **Build paths** | `/tmp/` prefix in DWARF debug info | Relative or normalized paths |

---

## C5 Integration with C4

Manifest includes build info + hashes. To verify:

1. Get manifest from owner (signed, verified by C4)
2. Check build_info fields (toolchain versions)
3. Rebuild using exact toolchain versions
4. Compare hashes to manifest
5. If all match: release is verifiable, no supply-chain tampering

This is the independent verification that C4 enables.

