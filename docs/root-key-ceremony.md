# Root Key Ceremony

Owner-generated asymmetric keypair for signing all NOVA modules and releases. The root key is created offline and **never stored on any NOVA machine**.

---

## Threat Model

The root key protects against:
- **Package tampering**: an attacker cannot forge a signature over a modified package
- **Module injection**: an attacker cannot add unsigned modules to a release
- **Rollback**: an attacker cannot replay an older, vulnerable version (version counter + signature)

The root key **does not protect against**:
- A compromised owner who willingly signs malicious code
- Physical tampering with a NOVA machine (see D5 threat model and C6 for Secure Boot)
- Network attacks on the distribution channel (signatures prove origin, not transport)

---

## Ceremony Overview

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  1. Offline machine (airgapped, no network)            │
│     └─ Generate Ed25519 keypair                        │
│     └─ Export public key only                          │
│     └─ Store private key in secure location            │
│                                                         │
│  2. On NOVA machine (or development machine)           │
│     └─ Import public key into trust root               │
│     └─ Every release: import signature from offline    │
│                                                         │
│  3. For each release:                                  │
│     └─ Build on connected machine                      │
│     └─ Export payload hash (reproducible build)        │
│     └─ Transfer to offline machine (USB, no network)   │
│     └─ Sign with private key (offline)                 │
│     └─ Export signature back to connected machine      │
│     └─ Verify signature before install                 │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## Prerequisites

- **Offline machine**: dedicated hardware, never connected to network
  - Linux, macOS, or Windows with an SSH client
  - Python 3.10+ with `cryptography` package
  - USB thumb drive or similar removable media for transferring hashes/signatures
  - Optionally: a printer (for paper backup of public key fingerprint)

- **Development/release machine**: connected, used for builds
  - The NOVA repository
  - Same Python setup as offline machine
  - Network access for builds (optional; reproducible build is same without network)

- **NOVA machine**: target deployment
  - The built NOVA image
  - Trust root with public key embedded or configured

---

## Step 1: Generate the Root Keypair (Offline Machine Only)

**Do this once. Store the private key in a secure location.**

### Using Python

```bash
python3 << 'EOF'
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import os

# Generate Ed25519 keypair
private_key = ed25519.Ed25519PrivateKey.generate()
public_key = private_key.public_key()

# Serialize private key (PEM/PKCS8 format, unencrypted)
# WARNING: protect this file with restrictive permissions
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

# Serialize public key (PEM format)
public_pem = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
)

# Write to files
os.makedirs("nova-root-key", exist_ok=True)
os.chmod("nova-root-key", 0o700)

with open("nova-root-key/root.priv", "wb") as f:
    f.write(private_pem)
os.chmod("nova-root-key/root.priv", 0o600)

with open("nova-root-key/root.pub", "wb") as f:
    f.write(public_pem)

# Print fingerprint
print("=== Root Key Generated ===")
print("\nPublic key fingerprint (sha256):")
import hashlib
pub_bytes = public_pem
fingerprint = hashlib.sha256(pub_bytes).hexdigest()
print(fingerprint)

print("\n=== SAVE THIS FINGERPRINT ===")
print("Print it and store offline.")
print("Verify it matches on every release.")

print("\nPublic key:")
print(public_pem.decode())
EOF
```

### Secure Storage

**Private key storage (choose one):**

1. **Encrypted USB drive** (recommended for portability)
   - Use LUKS/BitLocker encryption
   - Store only `root.priv` on the drive
   - Mount only on the offline machine
   - Unmount immediately after signing

2. **Dedicated offline machine** (maximum security)
   - Store `nova-root-key/` in a protected directory
   - `chmod 700 nova-root-key && chmod 600 nova-root-key/root.priv`
   - Never transfer the private key over any network

3. **Paper backup** (for disaster recovery only)
   - Print the private key PEM (in full, no truncation)
   - Store in a physical safe or vault
   - Laminate or use archival paper
   - Keep physically separated from the public key

**Never:**
- Store the private key on any machine with network access
- Use cloud storage (even encrypted)
- Share the private key with anyone, including the author
- Upload to version control
- Screenshot or photograph it without destroying the copy

---

## Step 2: Extract and Verify Public Key

**On development machine or NOVA machine.**

Transfer `root.pub` from the offline machine (USB, not network).

### Verify the fingerprint

```bash
python3 << 'EOF'
import hashlib

with open("root.pub", "rb") as f:
    pub_bytes = f.read()

fingerprint = hashlib.sha256(pub_bytes).hexdigest()
print(f"Public key SHA-256: {fingerprint}")
print("\nVerify this matches the fingerprint from Step 1.")
EOF
```

### Embed in NOVA trust root

Store `root.pub` at `kernel/trust/root-key.pub` in the repository:

```bash
mkdir -p kernel/trust
cp root.pub kernel/trust/root-key.pub
```

This file is part of every reproducible build and is committed to git.

---

## Step 3: Build and Export Hash

**On development/release machine.**

Build NOVA as normal:

```bash
cd kernel
cargo build --release
# or
bash scripts/check.sh
```

This produces `build/nova.hdd` (the release image).

Export the reproducible build hash:

```bash
python3 << 'EOF'
import hashlib
import sys

image_path = "build/nova.hdd"

with open(image_path, "rb") as f:
    data = f.read()

# Reproducible hash (no timestamps, no build paths)
digest = hashlib.sha256(data).hexdigest()

print(f"Release: nova.hdd")
print(f"Size: {len(data)} bytes")
print(f"SHA-256: {digest}")

# Write to file for signing
with open("nova-release-hash.txt", "w") as f:
    f.write(f"nova.hdd {digest}\n")
    f.write(f"version: 1.0.0\n")  # Bump for each release
    f.write(f"timestamp: {hashlib.sha256(f'{digest}' .encode()).hexdigest()[:16]}\n")

print("\nWrote to nova-release-hash.txt")
print("Transfer this file to offline machine for signing.")
EOF
```

Transfer `nova-release-hash.txt` to offline machine via USB.

---

## Step 4: Sign Release (Offline Machine Only)

**On offline machine with private key.**

```bash
python3 << 'EOF'
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ed25519
import base64

# Load private key
with open("nova-root-key/root.priv", "rb") as f:
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        serialization.load_pem_private_key(
            f.read(),
            password=None
        ).private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
    )

# Read release hash
with open("nova-release-hash.txt", "rb") as f:
    message = f.read()

# Sign
signature_bytes = private_key.sign(message)
signature_b64 = base64.b64encode(signature_bytes).decode()

print("=== Release Signed ===")
print(f"Signature (base64):\n{signature_b64}")

# Write to file
with open("nova-release-signature.txt", "w") as f:
    f.write(f"signature: {signature_b64}\n")
    f.write(f"algorithm: Ed25519\n")
    f.write(f"message-file: nova-release-hash.txt\n")

print("\nWrote signature to nova-release-signature.txt")
print("Transfer this file back to connected machine.")
EOF
```

Transfer `nova-release-signature.txt` back to development machine via USB.

---

## Step 5: Verify and Install

**On NOVA machine or development machine before deployment.**

```bash
python3 << 'EOF'
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import base64

# Load public key
with open("kernel/trust/root-key.pub", "rb") as f:
    public_key_pem = f.read()
    public_key = serialization.load_pem_public_key(public_key_pem)

# Load signature
with open("nova-release-signature.txt", "r") as f:
    lines = f.readlines()
    signature_b64 = lines[0].split(": ")[1].strip()
    signature_bytes = base64.b64decode(signature_b64)

# Load message (release hash)
with open("nova-release-hash.txt", "rb") as f:
    message = f.read()

# Verify
try:
    public_key.verify(signature_bytes, message)
    print("✓ Signature verified successfully!")
    print("\nRelease is authentic and ready to install.")
except Exception as e:
    print(f"✗ Signature verification failed: {e}")
    print("\nDO NOT INSTALL. The release may be tampered.")
    exit(1)
EOF
```

If verification passes, proceed with installation (see C3).

---

## Security Checklist

- [ ] Private key generated offline, never copied to any network-connected machine
- [ ] Public key fingerprint recorded on paper or printed backup
- [ ] Public key committed to repository at `kernel/trust/root-key.pub`
- [ ] Private key permissions set to `0600` (owner read/write only)
- [ ] Private key directory permissions set to `0700`
- [ ] Hash transfer uses USB or other offline media, never email/cloud/network
- [ ] Signature transfer uses USB or other offline media
- [ ] Each signature verified before installation
- [ ] Offline machine has no browser, email, or network access during key operations
- [ ] Private key backed up to secure location (safe, vault, or secondary encrypted device)

---

## Failure Modes

| Failure | Mitigation |
|---------|-----------|
| Private key accidentally deleted | Restore from paper backup or secondary encrypted device |
| Signature verification fails | Release may be corrupted in transit; re-sign on offline machine |
| Offline machine compromised (malware) | Assume private key is exposed; generate a new keypair and revoke old public key |
| Private key stolen | Immediately: (1) revoke public key, (2) generate new keypair, (3) resign and re-release all packages |
| USB drive lost | The drive is encrypted; private key on it is inaccessible without passphrase. Destroy the drive's passphrase and generate a new keypair. |

---

## Future Work (C2–C4)

**C2: Signed manifests** will embed the public key fingerprint and version counter, preventing replay attacks.

**C3: Atomic install** will use A/B slots with signature verification before boot handoff.

**C4: Extend signing backwards** will cover the Limine bootloader and kernel build inputs with the same root key.

---

## References

- Ed25519 (Bernstein et al.): https://en.wikipedia.org/wiki/EdDSA#Ed25519
- IETF RFC 8032 (Edwards-Curve Digital Signature Algorithm): https://tools.ietf.org/html/rfc8032
- Python cryptography library: https://cryptography.io/
- Signed Release Best Practices: https://wiki.debian.org/SecureApt

