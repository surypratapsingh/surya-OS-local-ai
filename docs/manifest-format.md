# Manifest Format — C2 Specification

Signed manifests for NOVA releases. Each release carries a manifest listing all packages, their hashes, versions, and capabilities.

---

## Purpose

A manifest proves:
- **Authenticity**: Signed by the owner's root key
- **Integrity**: Bit-level changes detected (SHA256 hashes)
- **Completeness**: All packages listed and accounted for
- **Versions**: Monotonic counters prevent rollback attacks
- **Permissions**: Each package declares what it can do (capabilities)
- **Freshness**: Timestamp and sequence number for replay protection

---

## Manifest Schema (JSON)

```json
{
  "manifest_version": "1.0",
  "release": {
    "version": "1.0.0",
    "timestamp": "2026-09-22T14:30:00Z",
    "release_sequence": 42,
    "description": "Stable release with mathd verifier"
  },
  "trust": {
    "algorithm": "Ed25519",
    "public_key_fingerprint": "sha256:abcd1234..."
  },
  "packages": [
    {
      "id": "kernel",
      "version": "1.0.0",
      "filename": "nova-kernel-1.0.0.bin",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "size": 2097152,
      "sequence": 1,
      "capabilities": [
        "boot:memory",
        "boot:cpu",
        "boot:filesystem"
      ]
    },
    {
      "id": "mathd",
      "version": "1.0.0",
      "filename": "nova-mathd-1.0.0.bin",
      "sha256": "2c26b46911185131006ba5991585e2955b1e27a5d977d4052eada67802735f12",
      "size": 1048576,
      "sequence": 2,
      "capabilities": [
        "compute:expression",
        "compute:derivative",
        "compute:verify"
      ]
    }
  ],
  "signature": "base64_encoded_ed25519_signature_here"
}
```

---

## Field Descriptions

### Manifest Header

| Field | Type | Purpose |
|-------|------|---------|
| `manifest_version` | String | Format version (currently "1.0") |

### Release Section

| Field | Type | Purpose | Constraints |
|-------|------|---------|-----------|
| `version` | String | Release version | Semantic versioning (X.Y.Z) |
| `timestamp` | ISO 8601 | Build timestamp | UTC, Z suffix required |
| `release_sequence` | Number | Monotonic counter | Must be > previous release |
| `description` | String | Human-readable notes | Max 200 chars, optional |

### Trust Section

| Field | Type | Purpose |
|-------|------|---------|
| `algorithm` | String | Signature algorithm | Must be "Ed25519" |
| `public_key_fingerprint` | String | SHA256 of public key | Format: "sha256:hexdigest" |

### Package Array

Each package object:

| Field | Type | Purpose | Example |
|-------|------|---------|---------|
| `id` | String | Package identifier | "kernel", "mathd" |
| `version` | String | Package version | Semantic versioning |
| `filename` | String | Distribution filename | "nova-kernel-1.0.0.bin" |
| `sha256` | String | Payload hash (hex) | 64 hex characters |
| `size` | Number | Bytes | For transport verification |
| `sequence` | Number | Package order | 1, 2, 3, ... (for boot sequence) |
| `capabilities` | Array | Permission list | ["boot:memory", "compute:*"] |

### Signature

| Field | Type | Purpose |
|-------|------|---------|
| `signature` | String | Ed25519 signature (base64) | Signs all fields above |

---

## Replay Protection

**Problem:** An attacker replays an old valid manifest, rolling back the system.

**Solution:** Three-layer defense

### Layer 1: Release Sequence Number

```
Release 1: release_sequence = 1
Release 2: release_sequence = 2
Release 3: release_sequence = 3
```

Only higher sequence numbers are accepted. A manifest with `release_sequence = 1` cannot overwrite `release_sequence = 3`.

### Layer 2: Timestamp

```
Release 1: timestamp = 2026-09-22T00:00:00Z
Release 2: timestamp = 2026-09-23T00:00:00Z
```

On install, verify `new_timestamp >= stored_timestamp`. This catches clocks set backwards and old manifests.

### Layer 3: Package Sequence

Within a manifest, packages are ordered:

```json
"packages": [
  {"id": "kernel", "sequence": 1, ...},
  {"id": "mathd", "sequence": 2, ...}
]
```

Sequence must be strictly increasing. If a manifest claims `mathd` at sequence 1 but kernel at sequence 1 (duplicate), it is rejected.

---

## Capabilities Scheme

Capabilities declare what each package is allowed to do.

### Format

Capabilities use a `namespace:permission` scheme:

```
boot:memory       — access physical memory
boot:cpu          — access CPU/scheduling
boot:filesystem   — access storage

compute:*         — all compute operations
compute:expression
compute:derivative
compute:verify

network:*         — all network operations (currently denied)

camera:*          — camera access
microphone:*      — microphone access
```

### Validation Rules

1. **Deny-by-default:** If a package doesn't declare a capability, it doesn't have it
2. **Explicit only:** No wildcard inheritance (kernel can't grant permissions)
3. **Scope:** Capabilities are runtime-enforced by the kernel/supervisor

### Example

Mathd verifier has compute capabilities but not network:

```json
{
  "id": "mathd",
  "capabilities": [
    "compute:expression",
    "compute:derivative",
    "compute:verify"
  ]
}
```

Attempting network access in mathd → kernel denies, no exception.

---

## Signing Process

### Step 1: Build Manifest (Connected Machine)

```python
manifest = {
    "manifest_version": "1.0",
    "release": {
        "version": "1.0.0",
        "timestamp": "2026-09-22T14:30:00Z",
        "release_sequence": next_sequence(),
        ...
    },
    "packages": [
        # Compute SHA256 of each payload
        # List capabilities
    ],
    "signature": ""  # Empty until signed
}
```

### Step 2: Canonicalize

Convert to deterministic JSON (sorted keys, minimal whitespace):

```python
canonical_manifest = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
```

This ensures the same manifest always produces the same bytes to sign.

### Step 3: Sign Offline

Transfer manifest to offline machine:

```bash
python3 sign-manifest.py nova-release-1.0.0.manifest nova-root-key/root.priv
```

Output: base64-encoded signature.

### Step 4: Embed Signature

```json
{
  ...
  "signature": "base64_encoded_signature_here"
}
```

### Step 5: Verify

On target machine:

```bash
python3 verify-manifest.py nova-release-1.0.0.manifest
```

---

## Version Counter Rules

| Scenario | Action |
|----------|--------|
| New release with `sequence = 42`, stored is `41` | ✓ Accept |
| New release with `sequence = 41`, stored is `42` | ✗ Reject (rollback) |
| New release with `sequence = 42`, stored is `42` | ✗ Reject (replay) |
| Timestamp newer but sequence lower | ✗ Reject (rollback attempt) |
| Timestamp older but sequence higher | ⚠️ Warn (clock skew) but accept |

---

## Files in Manifest

A release manifest may reference:

```json
"packages": [
  {
    "id": "kernel",
    "filename": "nova-kernel-1.0.0.bin",
    "sha256": "abc123...",
    "size": 2097152
  },
  {
    "id": "disk-image",
    "filename": "nova.hdd",
    "sha256": "def456...",
    "size": 1073741824
  }
]
```

For each `filename`, the installer verifies:
1. File exists
2. SHA256 matches manifest
3. Size matches manifest

If any check fails, installation is aborted.

---

## Manifest Storage

**On NOVA machine:**

```
/var/nova/
  ├─ manifests/
  │   ├─ 1.0.0.manifest  (current)
  │   ├─ 0.9.9.manifest  (previous, for rollback)
  │   └─ ...
  └─ packages/
      ├─ kernel-1.0.0.bin
      ├─ mathd-1.0.0.bin
      └─ ...
```

**Manifest permissions:** `0644` (world-readable, owner-writable)  
**Signature verification:** Mandatory before boot (no skipping)

---

## Manifest Versioning

The `manifest_version` field tracks the format itself:

- `manifest_version: "1.0"` — Current format
- `manifest_version: "2.0"` — Future breaking changes (e.g., new algorithm)

Older NOVA versions cannot boot manifests with newer `manifest_version` without explicit user consent (fail-safe).

---

## Example: Minimal Manifest

```json
{
  "manifest_version": "1.0",
  "release": {
    "version": "1.0.0",
    "timestamp": "2026-09-22T00:00:00Z",
    "release_sequence": 1,
    "description": "Initial release"
  },
  "trust": {
    "algorithm": "Ed25519",
    "public_key_fingerprint": "sha256:abcd1234567890abcd1234567890abcd1234567890abcd1234567890abcd1234"
  },
  "packages": [
    {
      "id": "kernel",
      "version": "1.0.0",
      "filename": "nova-kernel-1.0.0.bin",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "size": 2097152,
      "sequence": 1,
      "capabilities": ["boot:memory", "boot:cpu", "boot:filesystem"]
    }
  ],
  "signature": "G8VV0u9Q7K2pX3mW5nF8jH6kL4sZ9aB2cD5eF7gH9jK1mN3oP5qR7sT9uV1wX3yZ5"
}
```

---

## Verification Algorithm

```python
def verify_manifest(manifest_data: bytes, public_key: bytes) -> bool:
    """
    1. Parse JSON
    2. Extract signature
    3. Remove signature from dict
    4. Canonicalize remaining fields
    5. Verify signature over canonical bytes
    6. Check release_sequence >= stored
    7. Check timestamp >= stored
    8. Check package sequences are strictly increasing
    9. Verify all packages are present and checksums match
    """
```

---

## Integration with C1–C6

| Phase | Integration |
|-------|-------------|
| **C1** | Manifest is signed with root key from C1 ceremony |
| **C3** | Manifest is verified before A/B slot handoff |
| **C4** | Limine and kernel hashes added to manifest |
| **C5** | Release sequence bumped per reproducible build |
| **D2** | Network capabilities in manifest checked at runtime |
| **D3** | Manifest parser fuzz-tested |

---

## Security Properties

✅ **Authenticity:** Ed25519 signature proves manifest author  
✅ **Integrity:** SHA256 hashes detect any bit changes  
✅ **Ordering:** Sequence numbers prevent reordering attacks  
✅ **Replay:** Timestamp + sequence prevent old manifests  
✅ **Completeness:** All packages listed and signed as one unit  
✅ **Permissions:** Capabilities checked at runtime  

---

## What This Is Not

- Does not encrypt anything (see separate encryption design for sensitive data)
- Does not timestamp with external service (offline-first, owner controls clock)
- Does not auto-update (update requires owner action or explicit policy)
- Does not protect against malicious owner (signed by owner; D6 review required)

