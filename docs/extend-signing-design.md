# Extend Signing Backwards — C4 Specification

Unify the trust chain: root key → Limine → kernel → modules → packages.

---

## Problem Statement

**Current:** Only modules/packages are signed (C1–C3).

**Gap:** Bootloader (Limine) and kernel are unsigned. If either is compromised, trust chain is broken.

**Solution:** Extend signatures backwards to cover Limine and kernel in the same manifest.

---

## Trust Chain Architecture

```
┌─────────────────────────────────────────────────┐
│                                                 │
│  Owner's Root Key (C1)                          │
│    ↓ signs                                      │
│  Release Manifest (C2)                          │
│    ├─ Limine bootloader hash                    │
│    ├─ Kernel image hash                         │
│    ├─ mathd module hash                         │
│    ├─ ... (other packages)                      │
│    └─ Signature (Ed25519)                       │
│                                                 │
│  At boot:                                       │
│    1. Bootloader loads manifest (signed)        │
│    2. Verifies manifest signature (root key)    │
│    3. Reads Limine hash from manifest           │
│    4. Verifies running Limine matches hash      │
│    5. Reads kernel hash from manifest           │
│    6. Loads kernel from disk                    │
│    7. Verifies kernel hash matches              │
│    8. Boots kernel (kernel verifies modules)    │
│                                                 │
└─────────────────────────────────────────────────┘
```

---

## Extended Manifest Format

Manifest now includes Limine and kernel hashes:

```json
{
  "manifest_version": "1.0",
  "release": {
    "version": "1.0.0",
    "timestamp": "2026-09-22T14:30:00Z",
    "release_sequence": 42
  },
  "trust": {
    "algorithm": "Ed25519",
    "public_key_fingerprint": "sha256:abcd1234..."
  },
  "bootchain": {
    "limine": {
      "version": "12.9.0",
      "filename": "limine-12.9.0.bin",
      "sha256": "abc123...",
      "size": 262144,
      "capabilities": ["boot:firmware", "boot:memory"]
    },
    "kernel": {
      "id": "nova-kernel",
      "version": "1.0.0",
      "filename": "nova-kernel-1.0.0.bin",
      "sha256": "def456...",
      "size": 2097152,
      "capabilities": ["boot:cpu", "boot:scheduler", "boot:filesystem"]
    }
  },
  "packages": [
    {
      "id": "mathd",
      "version": "1.0.0",
      "filename": "nova-mathd-1.0.0.bin",
      "sha256": "ghi789...",
      "size": 1048576,
      "sequence": 1,
      "capabilities": ["compute:expression", "compute:derivative"]
    }
  ],
  "signature": "base64_encoded_ed25519_signature_here"
}
```

**Key additions:**
- `bootchain.limine` — Bootloader hash and capabilities
- `bootchain.kernel` — Kernel hash and capabilities
- Same signature covers all (Limine + kernel + packages as one unit)

---

## Signing Process (Build Machine)

### Step 1: Build Limine

```bash
# Standard Limine build
cd .freebuff/ref/limine/limine-12.9.0
make -j$(nproc)
cp limine.bin build/limine-12.9.0.bin
```

Compute hash:
```bash
sha256sum build/limine-12.9.0.bin
```

### Step 2: Build Kernel

```bash
cd kernel
cargo build --release
cp target/x86_64-unknown-none/release/nucleus build/nova-kernel-1.0.0.bin
```

Compute hash:
```bash
sha256sum build/nova-kernel-1.0.0.bin
```

### Step 3: Create Manifest with Bootchain

```bash
python3 create-manifest.py \
    --version 1.0.0 \
    --sequence 42 \
    --limine build/limine-12.9.0.bin \
    --kernel build/nova-kernel-1.0.0.bin \
    --package mathd:1.0.0:build/mathd.bin \
    --capabilities kernel:boot:cpu,boot:scheduler,boot:filesystem
```

### Step 4: Sign Offline

```bash
# Transfer manifest to offline machine
python3 sign-manifest.py nova-release-1.0.0.manifest nova-root-key/root.priv
```

### Step 5: Verify and Install

```bash
# Transfer signed manifest back
python3 verify-manifest.py nova-release-1.0.0.manifest

# Install (A/B slot swap)
python3 atomic-pointer-swap.py nova-release-1.0.0.manifest
```

---

## Boot Verification (Bootloader)

Limine must verify itself before handing off to kernel:

```c
// In Limine bootloader startup

// 1. Load manifest from disk (signed with root key)
manifest = load_manifest("releases/1.0.0.manifest");

// 2. Verify manifest signature
if (!verify_ed25519_signature(manifest, root_public_key)) {
    panic("Invalid manifest signature");
}

// 3. Get running Limine's hash (self-hash)
limine_hash = compute_sha256(limine_start, limine_end);

// 4. Compare to manifest
if (limine_hash != manifest.bootchain.limine.sha256) {
    panic("Limine hash mismatch (possible tampering)");
}

// 5. Load kernel
kernel = load_from_disk(manifest.bootchain.kernel.filename);

// 6. Verify kernel hash
kernel_hash = compute_sha256(kernel);
if (kernel_hash != manifest.bootchain.kernel.sha256) {
    panic("Kernel hash mismatch");
}

// 7. Hand off to kernel (verified)
jump_to_kernel(kernel);
```

---

## Kernel Verification (Kernel Entry)

Kernel verifies modules before execution:

```rust
// In kernel/main.rs

fn verify_packages(manifest: &Manifest) {
    for pkg in &manifest.packages {
        let loaded = load_package(&pkg.filename)?;
        let hash = sha256(&loaded);
        
        if hash != pkg.sha256 {
            panic!("Package {} hash mismatch", pkg.id);
        }
        
        // Verify capabilities (already enforced at runtime)
        assert_capabilities_valid(&pkg.capabilities)?;
    }
}
```

---

## Manifest Integrity: Limine Checks Manifest

### Problem
Limine can verify its own hash from the manifest. But who verifies the manifest itself?

### Solution
The manifest is signed. Limine verifies:
1. **Manifest signature** (Ed25519, using root public key)
2. **Manifest checksum** (to catch disk corruption)

### Implementation

```c
// Load manifest from disk
manifest_bytes = read_file("releases/1.0.0.manifest");
manifest_checksum = sha256(manifest_bytes);

// Verify against stored checksum (in pointer block)
pointer = read_pointer_block();
if (checksum != pointer.manifest_checksum) {
    // Manifest corrupted on disk, use backup
    manifest_bytes = read_backup_manifest();
}

// Verify signature
parsed = json_parse(manifest_bytes);
if (!verify_signature(parsed, root_public_key)) {
    panic("Manifest signature invalid");
}
```

---

## Build Inputs Signing (C4 Extension)

Extend further to sign build system configuration:

```json
{
  "build_inputs": {
    "kernel_config": {
      "file": "kernel/.config",
      "sha256": "aaa111...",
      "tool": "kconfig"
    },
    "limine_config": {
      "file": ".freebuff/ref/limine/limine.conf",
      "sha256": "bbb222...",
      "tool": "limine-config"
    },
    "cargo_lock": {
      "file": "kernel/Cargo.lock",
      "sha256": "ccc333...",
      "tool": "cargo"
    }
  }
}
```

**Purpose:** Prove that build configuration hasn't changed (detect supply-chain tampering).

**Verification:** In CI, recompute hashes and compare to manifest. If mismatch, build system was modified.

---

## Capabilities for Boot Components

### Limine Capabilities
```
boot:firmware   — access firmware interfaces
boot:memory     — read physical memory map
boot:disk       — access block devices
boot:efi        — call EFI services
boot:exit       — exit/reboot
```

### Kernel Capabilities
```
boot:cpu        — access CPU (scheduling, etc.)
boot:scheduler  — process scheduling
boot:filesystem — mount filesystems
boot:module     — load kernel modules
boot:memory     — manage virtual memory
```

### Module/Package Capabilities
```
compute:*       — math operations
network:*       — (denied)
camera:*        — (optional)
microphone:*    — (optional)
```

**Enforcement:** Runtime checks prevent operations outside declared capabilities.

---

## Security Properties

✅ **Authentic bootchain:** Root key signs Limine + kernel + packages as one unit  
✅ **Integrity:** SHA256 hashes detect tampering at each layer  
✅ **Ordering:** Manifest covers boot sequence  
✅ **Completeness:** All components (bootloader → kernel → modules) included  
✅ **Non-repudiation:** Signature proves who signed the release  

**Trust extends from:**
- Owner's offline root key
- → Manifest (signed, versioned, sequenced)
- → Limine (hash-verified by bootloader)
- → Kernel (hash-verified by Limine, verifies modules)
- → Modules/packages (verified by kernel)

---

## Integration with Reproducible Builds (C5)

To verify a release is what it claims:

1. **Download manifest** (signed)
2. **Verify signature** (root key)
3. **Rebuild from source** using inputs in manifest
4. **Compare hashes:**
   - Your built Limine should match manifest Limine hash
   - Your built kernel should match manifest kernel hash
   - If they don't match, release is not reproducible (bug or tampering)

This is the foundation of C5 (reproducible builds).

---

## Files Modified/Created

**Modified:**
- `kernel/limine.conf` — Boot with manifest verification
- `kernel/src/boot.rs` — Verify Limine and kernel hashes
- `tools/create-manifest.py` — Add --limine and --kernel options
- `tools/verify-manifest.py` — Verify bootchain hashes

**New:**
- `docs/extend-signing-design.md` — This file
- `tools/sign-bootchain.py` — Sign Limine + kernel
- `tools/test_extend_signing.py` — Bootchain tests

---

## Next Steps: C5 & C6

**C5 (Reproducible builds):**
- Bitwise identical builds for same inputs
- CI verifies hashes in manifest
- Allows anyone to independently verify build

**C6 (Secure Boot):**
- UEFI Secure Boot verifies Limine signature
- Hardware-level enforcement
- Optional (depends on threat model in D5)

