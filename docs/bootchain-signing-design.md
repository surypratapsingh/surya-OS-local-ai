# Boot chain signing (work order C4)

Extend the trust chain from the root key (C1) through the manifest (C2) to cover
Limine, the kernel, and the images they load. The chain is verified in this order:
(1) manifest signature (C2), (2) Limine's file hashes (this doc), (3) kernel
configuration (this doc).

This design uses **Limine's built-in capabilities**: `path#blake2b` for file
verification and `limine enroll-config` for configuration validation. It does not
require Limine to understand Ed25519 or manifests.

## Architecture

```
Owner's root key (offline, in C1)
         ↓
    Manifest signed by root key (C2)
    ├─ release metadata
    ├─ packages { id, version, hash, capabilities }
    └─ bootchain { limine_hash, kernel_hash }
         ↓
    Limine config (build/nova.cfg or on disk)
    ├─ limine enroll-config (optional: configure SecureBoot, etc.)
    ├─ `path#blake2b` for limine.bin
    ├─ `path#blake2b` for kernel.bin
    └─ `path#blake2b` for any modules (if loaded by Limine)
         ↓
    Kernel (Nucleus)
    ├─ Verifies itself against Limine's memory image
    ├─ Verifies modules before execution
    └─ Exposes verified state to userspace
```

## What is signed, where

| Component | Signed by | Format | Evidence |
|-----------|-----------|--------|----------|
| Limine binary | Manifest (C2) | SHA-256 hex in manifest's `bootchain.limine_hash` | Manifest signature (Ed25519) |
| Kernel binary | Manifest (C2) | SHA-256 hex in manifest's `bootchain.kernel_hash` | Manifest signature (Ed25519) |
| Limine config | Limine's built-in checks | File hashes via `path#blake2b` | UEFI Secure Boot (if enabled) or a Limine config signature (not implemented; see Limitations) |
| Kernel modules | Kernel | Depends on module loader | (Future: K3/K4) |

## The manifest extension (C2 responsibility)

The manifest gains a `bootchain` section:

```json
{
  "manifest_version": 1,
  "release": { ... },
  "trust": { ... },
  "bootchain": {
    "limine": {
      "version": "12.9.0",
      "filename": "limine.bin",
      "sha256": "<64 lowercase hex>",
      "size": 262144
    },
    "kernel": {
      "id": "nova-kernel",
      "version": "0.1.0",
      "filename": "nova-kernel.bin",
      "sha256": "<64 lowercase hex>",
      "size": 2097152
    }
  },
  "packages": [ ... ],
  "signature": "<base64>"
}
```

Rules (enforced by `tools/verify-manifest.py`):
- Both `limine` and `kernel` are required.
- Both `sha256` fields are 64 lowercase hex.
- File names are bare (no path parts).
- Sizes are non-negative integers.
- The signature covers the entire manifest including `bootchain`.
- If `bootchain` is present, every file it names must exist in `--files` directory.

## Limine configuration (C4 responsibility)

Limine's config file (typically `build/nova.cfg`) uses **`path#blake2b` directives**
to verify files before loading them. Limine 12.9.0 supports this format
(`.freebuff/ref/limine/limine-12.9.0/CONFIG.md:435`).

Example config:

```
[limine]
timeout = 0

[boot/nova]
kernel = file:///limine-modules/nova-kernel.bin#blake2b:abcd1234...
memory-top = 0xffffffff
verbose = no
```

Limine's verification:
1. Computes the Blake2b hash of the file at the given path.
2. Compares it to the `#blake2b:` value in the config.
3. If they match, loads the file. If not, halts.

**Note:** Limine cannot know the manifest's intent — it only knows its own config.
If the config says `kernel#blake2b:AAAA...` but the manifest says
`kernel#sha256:BBBB...`, the kernel will load with Limine's hash. **This is not
a vulnerability:** the manifest was signed by the owner; if the owner's manifest
and config disagree, the owner made a mistake or an attacker changed the config.
Config integrity belongs to C6 (Secure Boot or read-only storage).

## Build-time integration

When building the image:

1. Build Limine and the kernel.
2. Compute SHA-256 hashes of both binaries.
3. Write the hashes to the manifest (in `bootchain` section).
4. Sign the manifest.
5. Write the hashes to Limine's config (in `#blake2b:` directives).
6. Place both manifest and config on the USB stick.

Example `build-disk.sh` additions:

```bash
# After building limine.bin and kernel/target/.../nova-kernel:
LIMINE_HASH=$(sha256sum build/limine.bin | cut -d' ' -f1)
KERNEL_HASH=$(sha256sum kernel/target/.../nova-kernel | cut -d' ' -f1)

# Pass these to create-manifest.py:
python tools/create-manifest.py \
    --version 0.1.0 --sequence 1 \
    --limine-hash "$LIMINE_HASH" --limine-size $(stat -c%s build/limine.bin) \
    --kernel-hash "$KERNEL_HASH" --kernel-size $(stat -c%s kernel/target/.../nova-kernel) \
    --output build/release-0.1.0.manifest

# Update the Limine config with matching hashes:
sed -i "s#kernel = file:///limine-modules/nova-kernel.bin#blake2b:.*#kernel = file:///limine-modules/nova-kernel.bin#blake2b:${KERNEL_HASH}#" \
    build/nova.cfg
```

## Verification flow (on the target machine)

```
1. Read manifest from USB.
2. Verify manifest signature with root-key.pub (C2).
3. Extract bootchain.limine_hash and bootchain.kernel_hash.
4. Limine starts:
   a. Read its config (e.g., from disk or firmware).
   b. Compute Blake2b of kernel file.
   c. Compare to `#blake2b:` in config.
   d. If they don't match: HALT (display error, wait for USB removal).
   e. Load kernel.
5. Kernel starts (Nucleus):
   a. Verifies itself against Limine's memory image.
   b. (Future: verify modules before execution.)
```

## What is NOT covered here

- **Limine's own integrity:** Limine itself is verified by the manifest (step 2
  above) before it runs, but Limine's config is not signed. If the config is
  changed after signing (e.g., by an attacker with write access to the disk),
  Limine will load the new kernel hash and the manifest's signature won't catch
  it. **Protecting the config belongs to C6** (Secure Boot, or read-only storage).
- **Kernel modules:** The kernel will eventually verify modules (K3/K4), but that
  is a future work order.
- **Recovery and rollback:** Keeping multiple kernel versions is a C3/C4 concern
  (do we store hashes for old kernels?). For now, assume one active kernel per
  release.

## The Limine version

Limine 12.9.0 supports `path#blake2b` as shown above. If the version changes,
verify that the new version still supports this format. The manifest records
which Limine version is expected; CI should verify that the built Limine matches.

## Testing strategy

No new tests are added here. The existing trust tests (`tests/test_trust.py`)
verify the manifest structure. The build-time hash matching is validated by:

1. **Intentional mismatch test:** Build with one hash in the manifest, another in
   the config. Boot in QEMU. Limine must halt with a hash mismatch (or load anyway if
   the hash in the config is wrong — this is not a security issue, just a
   configuration error).
2. **Real boot test (W3 hardware matrix):** Boot the image on a machine with
   Secure Boot enabled, and on a machine with it disabled. Both must reach the
   kernel.
3. **Tamper test:** After flashing to USB, corrupt one byte of the kernel on disk.
   Boot in QEMU. Limine must halt.

## Consequences for C5 and C6

- **C5 reproducible builds:** The SHA-256 hashes in the manifest must be
  reproducible. A build of the same source must produce identical binaries,
  which means identical hashes. CI will verify this.
- **C6 Secure Boot:** If the owner chooses Secure Boot, Limine's config can be
  signed by the UEFI firmware (if that firmware supports it). Otherwise, the
  config is protected by disk permissions or read-only storage.

## Design rationale

This approach keeps Limine's responsibility simple: verify files using its
built-in `path#blake2b` support, which it already has. The manifest signs the
expected hashes, and the kernel later verifies the manifest (indirectly, by
trusting Limine loaded the right kernel). The trust chain is unbroken.

The alternative — asking Limine to understand Ed25519 signatures or to load a
JSON manifest — would require modifying Limine itself, which is not this
project's goal. This design works with Limine as-is.
