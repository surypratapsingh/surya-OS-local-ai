# Secure Boot — C6 Specification

UEFI Secure Boot with owner-controlled certificate. Hardware-verified boot chain.

---

## When to Use

**Threat model includes:** Physical attacker can modify disk/BIOS.

**When to skip:** Disk is protected, physical access controlled, or owner accepts risk of modified bootloader.

---

## Architecture

```
BIOS/UEFI firmware
  ├─ Has owner's public key certificate (in NVRAM)
  │
  ├─ Verifies Limine bootloader signature before execution
  │  (signature must be valid, else boot fails)
  │
  └─ Hands off to Limine (verified)

Limine bootloader
  ├─ Loads manifest (signed by owner root key, C1)
  ├─ Verifies manifest signature (C4)
  ├─ Reads kernel hash from manifest
  ├─ Loads kernel
  ├─ Verifies kernel hash matches (C4)
  └─ Hands off to kernel (verified)

Kernel
  ├─ Verifies modules (C4)
  └─ Boots with all layers verified

Result: Unbroken verified chain from firmware to kernel
```

---

## Owner-Controlled Certificate

Owner creates a keypair (offline, like C1):

```bash
# Offline machine
openssl req -new -x509 -newkey rsa:4096 -keyout owner-key.pem \
    -out owner-cert.pem -days 3650
# Creates: owner-cert.pem (public, goes to UEFI), owner-key.pem (private, never on network)
```

Certificate is enrolled in BIOS:

```bash
# On target machine (BIOS setup)
Import certificate: owner-cert.pem
Enable Secure Boot
Set mode: Custom (not Microsoft-only)
```

---

## Signing Limine for UEFI

Build process:

```bash
# Build Limine normally
make -j$(nproc)

# Sign with owner key (offline)
sbsign --key owner-key.pem --cert owner-cert.pem limine.bin

# Output: limine.bin.signed (ready to boot)
```

---

## Boot Verification

When machine powers on:

```
1. UEFI firmware starts
   ↓
2. Firmware loads Limine.bin (bootloader)
   ↓
3. Firmware verifies Limine.bin signature
   ├─ Uses owner-cert.pem (enrolled in NVRAM)
   ├─ Checks signature is valid
   └─ Rejects if invalid
   ↓
4. If valid, execute Limine
   ↓
5. Limine continues as normal (C4 verification)
```

---

## Integration with C1-C4

| Phase | Role |
|-------|------|
| **C1** | Owner generates signing key pair (offline) |
| **C4** | Manifest signed with root key (Limine + kernel hashes) |
| **C6** | Limine signed with owner key (hardware-verified) |

**Result:** Two layers of verification:
- Hardware verifies Limine signature (C6)
- Limine verifies manifest signature (C4) and kernel hash

If either fails, boot stops.

---

## Threat Model

**C6 protects against:**
- Attacker swaps Limine binary on disk → Signature fails, boot aborts
- BIOS modified to disable Secure Boot → Requires physical access to reset CMOS
- Kernel replaced → Manifest signature fails (C4)

**C6 does not protect against:**
- Attacker with full physical access (can reset BIOS, replace firmware chip)
- Compromised owner key (owner must keep private key offline)
- Supply-chain attack before C6 (C5 reproducible builds help here)

---

## Optional: Microsoft Secure Boot

Alternative to owner certificate: use Microsoft-signed bootloader (Shim).

**Trade-off:**
- ✅ Works on any machine with Secure Boot enabled
- ✗ Microsoft signs the bootloader (trust Microsoft)
- ✗ Less owner control

**Recommendation:** Use owner-controlled certificate (C6) for maximum control.

---

## Implementation Checklist

- [ ] Owner generates keypair offline (openssl)
- [ ] Enroll certificate in BIOS (UEFI setup menu)
- [ ] Add sbsign to build system
- [ ] Sign Limine in build pipeline
- [ ] Document in INSTALLATION.md
- [ ] Test on real hardware (2-3 machine types)
- [ ] Record results in docs/hardware-matrix.md

